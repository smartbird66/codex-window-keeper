from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from datetime import time as WallTime
from pathlib import Path

from codex_window_keeper.config import KeeperConfig
from codex_window_keeper.executor import CommandSpec
from codex_window_keeper.models import ExecutionResult, WindowInfo
from codex_window_keeper.service import (
    Decision,
    KeeperService,
    Phase,
    StepOutcome,
    decide_daily,
    decide_next,
)
from codex_window_keeper.state import KeeperState, load_state


class FakeClock:
    def __init__(self, now: float) -> None:
        self.current = now
        self.sleeps: list[float] = []
        self.woken = False

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds

    def wake(self) -> None:
        self.woken = True


class FakeExecutor:
    def __init__(self, returncodes: list[int]) -> None:
        self.returncodes = returncodes
        self.calls: list[tuple[CommandSpec, Path, float]] = []

    def run(
        self,
        command: CommandSpec,
        *,
        workdir: Path,
        timeout: float,
        stop_requested,  # type: ignore[no-untyped-def]
    ) -> ExecutionResult:
        self.calls.append((command, workdir, timeout))
        returncode = self.returncodes.pop(0)
        return ExecutionResult(returncode, 1, 2, timed_out=False)


def _window(reset: int) -> WindowInfo:
    return WindowInfo(300, 25.0, reset, Path("synthetic.jsonl"))


def _local_epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute).astimezone().timestamp()


def _decision(
    window: WindowInfo | None,
    state: KeeperState,
    *,
    now: float,
    grace: float = 10,
    poll: float = 20,
    retries: int = 3,
    retry_delay: float = 300,
) -> Decision:
    return decide_next(
        window,
        state,
        now=now,
        grace_seconds=grace,
        poll_seconds=poll,
        max_retries=retries,
        retry_delay_seconds=retry_delay,
    )


def _config(tmp_path: Path, **changes: object) -> KeeperConfig:
    base = KeeperConfig(
        mode="command",
        codex_home=tmp_path / ".codex",
        workdir=tmp_path,
        prompt="hello",
        session_id=None,
        command="/bin/true",
        grace_seconds=10,
        poll_seconds=20,
        max_session_files=50,
        tail_bytes=2 * 1024 * 1024,
        max_retries=3,
        retry_delay_seconds=300,
        command_timeout=30,
        state_file=tmp_path / "state.json",
        config_file=tmp_path / "config.toml",
        dry_run=False,
        once=True,
        status=False,
        verbose=0,
    )
    return replace(base, **changes)


def test_waits_for_status_when_no_window_exists() -> None:
    assert _decision(None, KeeperState(), now=100) == Decision(Phase.WAIT_FOR_STATUS, 20)


def test_waits_for_server_reset_timestamp() -> None:
    assert _decision(_window(150), KeeperState(), now=100) == Decision(
        Phase.WAIT_FOR_RESET, 20
    )


def test_waits_only_until_reset_when_poll_interval_is_longer() -> None:
    assert _decision(_window(105), KeeperState(), now=100) == Decision(Phase.WAIT_FOR_RESET, 5)


def test_applies_grace_period_after_reset() -> None:
    assert _decision(_window(100), KeeperState(), now=105) == Decision(Phase.GRACE_PERIOD, 5)


def test_executes_after_grace_period() -> None:
    decision = _decision(_window(100), KeeperState(), now=110)

    assert decision.phase == Phase.EXECUTE
    assert decision.execute


def test_already_triggered_reset_waits_for_new_real_value() -> None:
    state = KeeperState(last_seen_reset=100, last_triggered_reset=100, attempts=1)

    assert _decision(_window(100), state, now=20_000).phase == Phase.WAIT_NEW_RESET


def test_different_server_reset_value_starts_a_new_cycle() -> None:
    state = KeeperState(last_seen_reset=100, last_triggered_reset=100, attempts=1)

    assert _decision(_window(99), state, now=20_000).phase == Phase.EXECUTE


def test_retry_wait_uses_last_attempt_not_synthetic_window() -> None:
    state = KeeperState(last_seen_reset=100, attempts=1, last_attempt=120)

    assert _decision(_window(100), state, now=200).phase == Phase.RETRY
    assert _decision(_window(100), state, now=420).phase == Phase.EXECUTE


def test_retry_budget_means_initial_attempt_plus_retries() -> None:
    state = KeeperState(last_seen_reset=100, attempts=4, last_attempt=500)

    assert _decision(_window(100), state, now=1_000, retries=3).phase == Phase.RETRIES_EXHAUSTED


def test_daily_schedule_waits_until_local_time() -> None:
    now = datetime(2026, 9, 22, 5, 59, 50, tzinfo=UTC)

    decision = decide_daily(
        KeeperState(),
        now=now,
        daily_at=WallTime(6, 0),
        poll_seconds=20,
        max_retries=3,
        retry_delay_seconds=300,
    )

    assert decision == Decision(Phase.WAIT_FOR_DAILY_TIME, 10)


def test_daily_schedule_is_ready_at_or_after_local_time() -> None:
    decision = decide_daily(
        KeeperState(),
        now=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
        daily_at=WallTime(6, 0),
        poll_seconds=20,
        max_retries=3,
        retry_delay_seconds=300,
    )

    assert decision == Decision(
        Phase.EXECUTE,
        execute=True,
        schedule_date="2026-09-22",
    )


def test_completed_daily_schedule_waits_for_tomorrow() -> None:
    state = KeeperState(
        active_schedule_date="2026-09-22",
        last_scheduled_date="2026-09-22",
        attempts=1,
    )

    decision = decide_daily(
        state,
        now=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
        daily_at=WallTime(6, 0),
        poll_seconds=20,
        max_retries=3,
        retry_delay_seconds=300,
    )

    assert decision.phase == Phase.WAIT_FOR_DAILY_TIME
    assert decision.sleep_seconds == 20


def test_daily_retry_uses_persisted_attempt_time() -> None:
    attempted = datetime(2026, 9, 22, 6, 0, tzinfo=UTC).timestamp()
    state = KeeperState(
        active_schedule_date="2026-09-22",
        attempts=1,
        last_attempt=attempted,
    )

    waiting = decide_daily(
        state,
        now=datetime(2026, 9, 22, 6, 1, tzinfo=UTC),
        daily_at=WallTime(6, 0),
        poll_seconds=20,
        max_retries=3,
        retry_delay_seconds=300,
    )
    ready = decide_daily(
        state,
        now=datetime(2026, 9, 22, 6, 5, tzinfo=UTC),
        daily_at=WallTime(6, 0),
        poll_seconds=20,
        max_retries=3,
        retry_delay_seconds=300,
    )

    assert waiting.phase == Phase.RETRY
    assert ready.phase == Phase.EXECUTE


def test_success_is_persisted_before_once_exits(tmp_path: Path) -> None:
    clock = FakeClock(200)
    executor = FakeExecutor([0])
    service = KeeperService(
        _config(tmp_path, grace_seconds=0),
        detector=lambda: _window(100),
        executor=executor,
        clock=clock,
    )

    assert service.run(install_signal_handlers=False) == 0

    persisted = load_state(tmp_path / "state.json")
    assert persisted.last_triggered_reset == 100
    assert persisted.attempts == 1
    assert len(executor.calls) == 1


def test_restart_does_not_execute_same_reset_twice(tmp_path: Path) -> None:
    first_executor = FakeExecutor([0])
    config = _config(tmp_path, grace_seconds=0)
    first = KeeperService(
        config,
        detector=lambda: _window(100),
        executor=first_executor,
        clock=FakeClock(200),
    )
    assert first.run(install_signal_handlers=False) == 0

    second_executor = FakeExecutor([])
    second_clock = FakeClock(200)
    restarted = KeeperService(
        replace(config, once=False),
        detector=lambda: _window(100),
        executor=second_executor,
        clock=second_clock,
    )

    assert restarted.step() == StepOutcome.CONTINUE
    assert restarted.phase == Phase.WAIT_NEW_RESET
    assert not second_executor.calls


def test_new_reset_can_execute_after_previous_success(tmp_path: Path) -> None:
    config = _config(tmp_path, grace_seconds=0)
    first = KeeperService(
        config,
        detector=lambda: _window(100),
        executor=FakeExecutor([0]),
        clock=FakeClock(200),
    )
    assert first.run(install_signal_handlers=False) == 0

    executor = FakeExecutor([0])
    second = KeeperService(
        config,
        detector=lambda: _window(300),
        executor=executor,
        clock=FakeClock(400),
    )

    assert second.run(install_signal_handlers=False) == 0
    assert len(executor.calls) == 1
    assert load_state(config.state_file).last_triggered_reset == 300


def test_once_retries_then_returns_failure(tmp_path: Path) -> None:
    executor = FakeExecutor([9, 9, 9])
    clock = FakeClock(200)
    service = KeeperService(
        _config(
            tmp_path,
            grace_seconds=0,
            poll_seconds=10,
            max_retries=2,
            retry_delay_seconds=5,
        ),
        detector=lambda: _window(100),
        executor=executor,
        clock=clock,
    )

    assert service.run(install_signal_handlers=False) == 1
    assert len(executor.calls) == 3
    assert load_state(tmp_path / "state.json").attempts == 3


def test_dry_run_never_executes_or_writes_state(tmp_path: Path) -> None:
    executor = FakeExecutor([])
    service = KeeperService(
        _config(tmp_path, grace_seconds=0, dry_run=True),
        detector=lambda: _window(100),
        executor=executor,
        clock=FakeClock(200),
    )

    assert service.run(install_signal_handlers=False) == 0
    assert not executor.calls
    assert not (tmp_path / "state.json").exists()


def test_daily_service_executes_once_without_scanning_sessions(tmp_path: Path) -> None:
    executor = FakeExecutor([0])

    def unexpected_detector() -> WindowInfo | None:
        raise AssertionError("daily scheduling must not scan session files")

    service = KeeperService(
        _config(tmp_path, daily_at="06:00"),
        detector=unexpected_detector,
        executor=executor,
        clock=FakeClock(_local_epoch(2026, 9, 22, 6, 0)),
    )

    assert service.run(install_signal_handlers=False) == 0
    assert len(executor.calls) == 1
    persisted = load_state(tmp_path / "state.json")
    assert persisted.last_scheduled_date == "2026-09-22"
    assert persisted.attempts == 1


def test_daily_service_restart_does_not_repeat_same_date(tmp_path: Path) -> None:
    config = _config(tmp_path, daily_at="06:00")
    first = KeeperService(
        config,
        detector=lambda: None,
        executor=FakeExecutor([0]),
        clock=FakeClock(_local_epoch(2026, 9, 22, 6, 0)),
    )
    assert first.run(install_signal_handlers=False) == 0

    executor = FakeExecutor([])
    restarted = KeeperService(
        replace(config, once=False),
        detector=lambda: None,
        executor=executor,
        clock=FakeClock(_local_epoch(2026, 9, 22, 9, 0)),
    )

    assert restarted.step() == StepOutcome.CONTINUE
    assert restarted.phase == Phase.WAIT_FOR_DAILY_TIME
    assert not executor.calls


def test_daily_service_allows_next_calendar_date(tmp_path: Path) -> None:
    config = _config(tmp_path, daily_at="06:00")
    first = KeeperService(
        config,
        detector=lambda: None,
        executor=FakeExecutor([0]),
        clock=FakeClock(_local_epoch(2026, 9, 22, 6, 0)),
    )
    assert first.run(install_signal_handlers=False) == 0

    executor = FakeExecutor([0])
    next_day = KeeperService(
        config,
        detector=lambda: None,
        executor=executor,
        clock=FakeClock(_local_epoch(2026, 9, 23, 6, 0)),
    )

    assert next_day.run(install_signal_handlers=False) == 0
    assert len(executor.calls) == 1
    assert load_state(config.state_file).last_scheduled_date == "2026-09-23"


def test_daily_service_retries_then_fails_once(tmp_path: Path) -> None:
    executor = FakeExecutor([9, 9])
    service = KeeperService(
        _config(
            tmp_path,
            daily_at="06:00",
            poll_seconds=10,
            max_retries=1,
            retry_delay_seconds=5,
        ),
        detector=lambda: None,
        executor=executor,
        clock=FakeClock(_local_epoch(2026, 9, 22, 6, 0)),
    )

    assert service.run(install_signal_handlers=False) == 1
    assert len(executor.calls) == 2
    assert load_state(tmp_path / "state.json").attempts == 2


def test_daily_dry_run_has_no_persistent_side_effects(tmp_path: Path) -> None:
    executor = FakeExecutor([])
    service = KeeperService(
        _config(tmp_path, daily_at="06:00", dry_run=True),
        detector=lambda: None,
        executor=executor,
        clock=FakeClock(_local_epoch(2026, 9, 22, 6, 0)),
    )

    assert service.run(install_signal_handlers=False) == 0
    assert not executor.calls
    assert not (tmp_path / "state.json").exists()


def test_request_stop_wakes_sleeping_clock(tmp_path: Path) -> None:
    clock = FakeClock(100)
    service = KeeperService(
        _config(tmp_path), detector=lambda: None, executor=FakeExecutor([]), clock=clock
    )

    service.request_stop()

    assert clock.woken
    assert service.step() == StepOutcome.STOPPED
