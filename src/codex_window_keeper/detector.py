"""Detection of Codex rate-limit metadata in local session files.

Codex session files are private. This module only returns rate-limit metadata;
it never returns or logs raw session records.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .models import WindowInfo

LOGGER = logging.getLogger(__name__)
FIVE_HOUR_WINDOW_MINUTES = 300


def discover_session_files(codex_home: Path, max_files: int) -> list[Path]:
    """Return the most recently modified Codex session files."""
    if max_files <= 0:
        raise ValueError("max_files must be greater than zero")

    session_root = codex_home.expanduser() / "sessions"
    if not session_root.is_dir():
        return []

    candidates: list[tuple[int, str, Path]] = []
    try:
        paths = session_root.rglob("*.jsonl")
        for path in paths:
            try:
                if path.is_file():
                    stat = path.stat()
                    candidates.append((stat.st_mtime_ns, str(path), path))
            except OSError as exc:
                LOGGER.debug("Could not inspect session file %s: %s", path, exc)
    except OSError as exc:
        LOGGER.warning("Could not scan Codex session directory %s: %s", session_root, exc)
        return []

    candidates.sort(reverse=True)
    return [item[2] for item in candidates[:max_files]]


def _tail_lines(path: Path, tail_bytes: int) -> list[bytes]:
    """Read at most ``tail_bytes`` from the end, excluding a partial first line."""
    if tail_bytes <= 0:
        raise ValueError("tail_bytes must be greater than zero")

    with path.open("rb") as session_file:
        size = session_file.seek(0, 2)
        offset = max(0, size - tail_bytes)
        session_file.seek(offset)
        data = session_file.read(tail_bytes)
        if offset:
            # The seek will usually land in the middle of a JSONL record. Skipping
            # one line can occasionally discard a complete record at the boundary,
            # which is preferable to ever treating a fragment as valid JSON.
            newline = data.find(b"\n")
            if newline < 0:
                return []
            data = data[newline + 1 :]
        return data.splitlines()


def _walk_dicts(value: object) -> Iterator[dict[str, Any]]:
    """Yield dictionaries recursively without making schema-wide assumptions."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _extract_window(record: object, source_file: Path) -> WindowInfo | None:
    for container in _walk_dicts(record):
        rate_limits = container.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        primary = rate_limits.get("primary")
        if not isinstance(primary, dict):
            continue

        window = _number(primary.get("window_minutes"))
        reset = _number(primary.get("resets_at"))
        if window != FIVE_HOUR_WINDOW_MINUTES or reset is None or reset <= 0:
            continue
        if not reset.is_integer():
            continue

        used = _number(primary.get("used_percent"))
        return WindowInfo(
            window_minutes=FIVE_HOUR_WINDOW_MINUTES,
            used_percent=used,
            resets_at=int(reset),
            source_file=source_file,
        )
    return None


def detect_window_in_file(path: Path, tail_bytes: int) -> WindowInfo | None:
    """Find the newest valid five-hour window event in one JSONL file."""
    try:
        lines = _tail_lines(path, tail_bytes)
    except OSError as exc:
        LOGGER.debug("Could not read session file %s: %s", path, exc)
        return None

    for raw_line in reversed(lines):
        try:
            record = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        window = _extract_window(record, path)
        if window is not None:
            return window
    return None


def detect_latest_window(
    codex_home: Path,
    max_files: int = 50,
    tail_bytes: int = 2 * 1024 * 1024,
) -> WindowInfo | None:
    """Find the newest usable Codex five-hour window metadata."""
    for path in discover_session_files(codex_home, max_files):
        window = detect_window_in_file(path, tail_bytes)
        if window is not None:
            return window
    return None
