"""Crash-safe persistent trigger state."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class KeeperState:
    """Small, non-sensitive state used to suppress duplicate triggers."""

    last_seen_reset: int | None = None
    last_triggered_reset: int | None = None
    attempts: int = 0
    last_attempt: float | None = None
    active_schedule_date: str | None = None
    last_scheduled_date: str | None = None


def _optional_number(data: dict[str, Any], key: str, kind: type[int] | type[float]) -> Any:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} has an invalid type")
    if not math.isfinite(float(value)):
        raise ValueError(f"{key} is not finite")
    if kind is int:
        if not float(value).is_integer():
            raise ValueError(f"{key} is not an integer")
        return int(value)
    return float(value)


def _optional_date(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} has an invalid type")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key} is not an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{key} is not a canonical ISO date")
    return value


def load_state(path: Path) -> KeeperState:
    """Load state, falling back safely if it is absent or damaged."""
    try:
        with path.open(encoding="utf-8") as state_file:
            data = json.load(state_file)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
        attempts = _optional_number(data, "attempts", int)
        attempts = 0 if attempts is None else attempts
        if attempts < 0:
            raise ValueError("attempts is negative")
        return KeeperState(
            last_seen_reset=_optional_number(data, "last_seen_reset", int),
            last_triggered_reset=_optional_number(data, "last_triggered_reset", int),
            attempts=attempts,
            last_attempt=_optional_number(data, "last_attempt", float),
            active_schedule_date=_optional_date(data, "active_schedule_date"),
            last_scheduled_date=_optional_date(data, "last_scheduled_date"),
        )
    except FileNotFoundError:
        return KeeperState()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # Never print file contents: even though the keeper only writes timestamps,
        # users may point --state-file at an unexpected file by mistake.
        LOGGER.warning("Ignoring unreadable keeper state file %s: %s", path, exc)
        return KeeperState()


def save_state(path: Path, state: KeeperState) -> None:
    """Atomically persist state using a same-directory temporary file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            os.chmod(temporary_name, 0o600)
            json.dump(asdict(state), temporary, sort_keys=True, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None

        # Persist the rename itself on filesystems that support directory fsync.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            LOGGER.debug("Directory fsync is unavailable for %s", path.parent)
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                Path(temporary_name).unlink()


def observe_reset(state: KeeperState, resets_at: int) -> bool:
    """Record a newly observed reset and clear retry accounting for it."""
    if state.last_seen_reset == resets_at and state.active_schedule_date is None:
        return False
    state.last_seen_reset = resets_at
    state.active_schedule_date = None
    state.attempts = 0
    state.last_attempt = None
    return True


def mark_attempt(state: KeeperState, resets_at: int, attempted_at: float) -> None:
    """Record an execution attempt before starting the child process."""
    if state.last_seen_reset != resets_at:
        observe_reset(state, resets_at)
    state.attempts += 1
    state.last_attempt = attempted_at


def mark_triggered(state: KeeperState, resets_at: int) -> None:
    """Record successful handling of one exact server-provided reset timestamp."""
    state.last_seen_reset = resets_at
    state.last_triggered_reset = resets_at
    state.active_schedule_date = None


def was_triggered(state: KeeperState, resets_at: int) -> bool:
    """Return whether this exact reset has already triggered successfully."""
    return state.last_triggered_reset == resets_at


def observe_schedule_date(state: KeeperState, schedule_date: str) -> bool:
    """Start retry accounting for a new local calendar date."""
    if state.active_schedule_date == schedule_date and state.last_seen_reset is None:
        return False
    state.active_schedule_date = schedule_date
    state.last_seen_reset = None
    state.attempts = 0
    state.last_attempt = None
    return True


def mark_schedule_attempt(
    state: KeeperState, schedule_date: str, attempted_at: float
) -> None:
    """Record one daily scheduled execution attempt before it starts."""
    if state.active_schedule_date != schedule_date:
        observe_schedule_date(state, schedule_date)
    state.attempts += 1
    state.last_attempt = attempted_at


def mark_schedule_triggered(state: KeeperState, schedule_date: str) -> None:
    """Record a successful daily trigger for duplicate suppression."""
    state.active_schedule_date = schedule_date
    state.last_scheduled_date = schedule_date
    state.last_seen_reset = None
