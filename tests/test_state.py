from __future__ import annotations

import json
from pathlib import Path

from codex_window_keeper.state import (
    KeeperState,
    load_state,
    mark_attempt,
    mark_schedule_attempt,
    mark_schedule_triggered,
    mark_triggered,
    observe_reset,
    observe_schedule_date,
    save_state,
    was_triggered,
)


def test_missing_state_file_uses_defaults(tmp_path: Path) -> None:
    assert load_state(tmp_path / "missing.json") == KeeperState()


def test_state_round_trip_and_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    expected = KeeperState(
        last_seen_reset=1_800_000_000,
        last_triggered_reset=1_700_000_000,
        attempts=2,
        last_attempt=1_800_000_010.5,
        active_schedule_date="2026-09-22",
        last_scheduled_date="2026-09-21",
    )

    save_state(path, expected)

    assert load_state(path) == expected
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_corrupt_state_is_ignored(tmp_path: Path, caplog: object) -> None:
    path = tmp_path / "state.json"
    path.write_text("not json", encoding="utf-8")

    assert load_state(path) == KeeperState()


def test_successful_reset_is_not_triggered_again_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = KeeperState()
    observe_reset(state, 1_800_000_000)
    mark_attempt(state, 1_800_000_000, 1_800_000_010)
    mark_triggered(state, 1_800_000_000)
    save_state(path, state)

    restarted = load_state(path)

    assert was_triggered(restarted, 1_800_000_000)
    assert restarted.attempts == 1


def test_new_reset_clears_attempts_and_can_trigger() -> None:
    state = KeeperState(
        last_seen_reset=1_800_000_000,
        last_triggered_reset=1_800_000_000,
        attempts=3,
        last_attempt=1_800_000_010,
    )

    assert observe_reset(state, 1_800_018_000)

    assert not was_triggered(state, 1_800_018_000)
    assert state.attempts == 0
    assert state.last_attempt is None


def test_observing_same_reset_preserves_retry_state() -> None:
    state = KeeperState(last_seen_reset=123, attempts=2, last_attempt=456.0)

    assert not observe_reset(state, 123)
    assert state.attempts == 2
    assert state.last_attempt == 456.0


def test_attempt_is_recorded_before_success() -> None:
    state = KeeperState()

    mark_attempt(state, resets_at=123, attempted_at=456.5)

    assert state.last_seen_reset == 123
    assert state.attempts == 1
    assert state.last_attempt == 456.5
    assert state.last_triggered_reset is None


def test_invalid_state_values_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"attempts": -1}), encoding="utf-8")

    assert load_state(path) == KeeperState()


def test_daily_schedule_is_not_triggered_twice_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = KeeperState()
    mark_schedule_attempt(state, "2026-09-22", 1_800_000_000)
    mark_schedule_triggered(state, "2026-09-22")
    save_state(path, state)

    restarted = load_state(path)

    assert restarted.last_scheduled_date == "2026-09-22"
    assert restarted.active_schedule_date == "2026-09-22"
    assert restarted.attempts == 1


def test_new_schedule_date_resets_attempts_and_switches_retry_context() -> None:
    state = KeeperState(
        last_seen_reset=123,
        attempts=3,
        last_attempt=456,
        active_schedule_date=None,
        last_scheduled_date="2026-09-21",
    )

    assert observe_schedule_date(state, "2026-09-22")

    assert state.last_seen_reset is None
    assert state.active_schedule_date == "2026-09-22"
    assert state.attempts == 0
    assert state.last_attempt is None


def test_invalid_daily_date_in_state_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"last_scheduled_date":"22-09-2026"}', encoding="utf-8")

    assert load_state(path) == KeeperState()
