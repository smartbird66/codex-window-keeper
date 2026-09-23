"""Long-running, testable keeper state machine."""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as WallTime
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Protocol

from .config import KeeperConfig
from .detector import detect_latest_window
from .executor import CommandExecutor, CommandSpec, build_command
from .models import ExecutionResult, WindowInfo
from .state import (
    KeeperState,
    load_state,
    mark_attempt,
    mark_schedule_attempt,
    mark_schedule_triggered,
    mark_triggered,
    observe_reset,
    save_state,
)

LOGGER = logging.getLogger(__name__)


class Phase(StrEnum):
    WAIT_FOR_STATUS = "WAIT_FOR_STATUS"
    WAIT_FOR_RESET = "WAIT_FOR_RESET"
    GRACE_PERIOD = "GRACE_PERIOD"
    EXECUTE = "EXECUTE"
    WAIT_NEW_RESET = "WAIT_NEW_RESET"
    RETRY = "RETRY"
    RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"
    WAIT_FOR_DAILY_TIME = "WAIT_FOR_DAILY_TIME"


@dataclass(frozen=True, slots=True)
class Decision:
    """One pure state-machine decision."""

    phase: Phase
    sleep_seconds: float = 0
    execute: bool = False
    schedule_date: str | None = None


class StepOutcome(StrEnum):
    CONTINUE = "CONTINUE"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    STOPPED = "STOPPED"


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    def wake(self) -> None: ...


class Executor(Protocol):
    def run(
        self,
        command: CommandSpec,
        *,
        workdir: Path,
        timeout: float,
        stop_requested: Callable[[], bool],
    ) -> ExecutionResult: ...


class RealClock:
    """Wall clock whose sleeps can be interrupted by SIGINT/SIGTERM."""

    def __init__(self) -> None:
        self._wake_event = threading.Event()

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        self._wake_event.wait(max(0, seconds))
        self._wake_event.clear()

    def wake(self) -> None:
        self._wake_event.set()


def decide_next(
    window: WindowInfo | None,
    state: KeeperState,
    *,
    now: float,
    grace_seconds: float,
    poll_seconds: float,
    max_retries: int,
    retry_delay_seconds: float,
) -> Decision:
    """Return the next action without sleeping, executing, or mutating state."""
    if window is None:
        return Decision(Phase.WAIT_FOR_STATUS, sleep_seconds=poll_seconds)

    reset = window.resets_at
    if reset == state.last_triggered_reset:
        return Decision(Phase.WAIT_NEW_RESET, sleep_seconds=poll_seconds)

    if now < reset:
        return Decision(Phase.WAIT_FOR_RESET, sleep_seconds=min(poll_seconds, reset - now))

    trigger_at = reset + grace_seconds
    if now < trigger_at:
        return Decision(Phase.GRACE_PERIOD, sleep_seconds=min(poll_seconds, trigger_at - now))

    attempts = state.attempts if state.last_seen_reset == reset else 0
    last_attempt = state.last_attempt if state.last_seen_reset == reset else None
    if attempts > max_retries:
        return Decision(Phase.RETRIES_EXHAUSTED, sleep_seconds=poll_seconds)

    if attempts and last_attempt is not None:
        retry_at = last_attempt + retry_delay_seconds
        if now < retry_at:
            return Decision(Phase.RETRY, sleep_seconds=min(poll_seconds, retry_at - now))

    return Decision(Phase.EXECUTE, execute=True)


def parse_daily_time(value: str) -> WallTime:
    """Convert a normalized HH:MM configuration value to a wall-clock time."""
    hour, minute = (int(part) for part in value.split(":"))
    return WallTime(hour=hour, minute=minute)


def daily_target(now: datetime, daily_at: WallTime, target_date: date) -> datetime:
    """Build a local scheduled datetime using the current local timezone."""
    return datetime.combine(target_date, daily_at, tzinfo=now.tzinfo)


def decide_daily(
    state: KeeperState,
    *,
    now: datetime,
    daily_at: WallTime,
    poll_seconds: float,
    max_retries: int,
    retry_delay_seconds: float,
) -> Decision:
    """Decide a daily local-time trigger without consulting Codex session data."""
    today = now.date()
    today_key = today.isoformat()
    scheduled_today = daily_target(now, daily_at, today)

    if state.last_scheduled_date == today_key:
        scheduled_tomorrow = daily_target(now, daily_at, today + timedelta(days=1))
        return Decision(
            Phase.WAIT_FOR_DAILY_TIME,
            sleep_seconds=min(poll_seconds, max(0, (scheduled_tomorrow - now).total_seconds())),
        )

    if now < scheduled_today:
        return Decision(
            Phase.WAIT_FOR_DAILY_TIME,
            sleep_seconds=min(poll_seconds, (scheduled_today - now).total_seconds()),
        )

    attempts = state.attempts if state.active_schedule_date == today_key else 0
    last_attempt = state.last_attempt if state.active_schedule_date == today_key else None
    if attempts > max_retries:
        scheduled_tomorrow = daily_target(now, daily_at, today + timedelta(days=1))
        return Decision(
            Phase.RETRIES_EXHAUSTED,
            sleep_seconds=min(poll_seconds, max(0, (scheduled_tomorrow - now).total_seconds())),
            schedule_date=today_key,
        )

    if attempts and last_attempt is not None:
        retry_at = last_attempt + retry_delay_seconds
        remaining = retry_at - now.timestamp()
        if remaining > 0:
            return Decision(
                Phase.RETRY,
                sleep_seconds=min(poll_seconds, remaining),
                schedule_date=today_key,
            )

    return Decision(Phase.EXECUTE, execute=True, schedule_date=today_key)


class KeeperService:
    """Orchestrate detection, durable state, waiting, and command execution."""

    def __init__(
        self,
        config: KeeperConfig,
        *,
        detector: Callable[[], WindowInfo | None] | None = None,
        executor: Executor | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.clock = clock or RealClock()
        self.executor = executor or CommandExecutor()
        self.detector = detector or self._detect
        self.command = build_command(config)
        self.daily_time = parse_daily_time(config.daily_at) if config.daily_at is not None else None
        self.state = load_state(config.state_file)
        self.phase: Phase | None = None
        self.stop_requested = False

    def _detect(self) -> WindowInfo | None:
        return detect_latest_window(
            self.config.codex_home,
            max_files=self.config.max_session_files,
            tail_bytes=self.config.tail_bytes,
        )

    def request_stop(self) -> None:
        self.stop_requested = True
        self.clock.wake()

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("Received signal %s; stopping", signal.Signals(signum).name)
        self.request_stop()

    def _persist(self) -> None:
        if not self.config.dry_run:
            save_state(self.config.state_file, self.state)

    def _log_phase(self, decision: Decision, window: WindowInfo | None) -> None:
        if decision.phase == self.phase:
            return
        self.phase = decision.phase
        if decision.phase == Phase.WAIT_FOR_STATUS:
            LOGGER.warning(
                "No valid 300-minute Codex usage window was detected. "
                "Waiting for Codex activity; run --status --verbose for diagnostics."
            )
        elif decision.phase == Phase.WAIT_FOR_RESET and window is not None:
            LOGGER.info("Detected 5h reset timestamp: %s", window.resets_at)
        elif decision.phase == Phase.GRACE_PERIOD:
            LOGGER.info("Usage window reset detected; waiting for grace period")
        elif decision.phase == Phase.WAIT_NEW_RESET:
            LOGGER.info("Waiting for Codex to write a new resets_at value")
        elif decision.phase == Phase.RETRY:
            LOGGER.info("Waiting before command retry")
        elif decision.phase == Phase.RETRIES_EXHAUSTED:
            if self.config.daily_at is None:
                LOGGER.error("Command retries exhausted; waiting for a new resets_at value")
            else:
                LOGGER.error("Daily command retries exhausted; waiting for tomorrow")
        elif decision.phase == Phase.WAIT_FOR_DAILY_TIME:
            LOGGER.info("Waiting for daily trigger at %s local time", self.config.daily_at)

    def _execute(
        self,
        *,
        window: WindowInfo | None = None,
        schedule_date: str | None = None,
    ) -> StepOutcome:
        if (window is None) == (schedule_date is None):
            raise ValueError("exactly one execution trigger must be supplied")
        if self.config.dry_run:
            LOGGER.info("[DRY RUN] Would execute:\n\n%s", self.command.display)
            if window is not None:
                mark_triggered(self.state, window.resets_at)
            else:
                assert schedule_date is not None
                mark_schedule_triggered(self.state, schedule_date)
            return StepOutcome.SUCCESS if self.config.once else StepOutcome.CONTINUE

        attempted_at = self.clock.now()
        if window is not None:
            mark_attempt(self.state, window.resets_at, attempted_at)
        else:
            assert schedule_date is not None
            mark_schedule_attempt(self.state, schedule_date, attempted_at)
        # State must reach stable storage before the child starts. If persistence
        # fails, execution is aborted rather than risking an untracked duplicate.
        self._persist()
        LOGGER.info("Executing: %s", self.command.log_display)
        result = self.executor.run(
            self.command,
            workdir=self.config.workdir,
            timeout=self.config.command_timeout,
            stop_requested=lambda: self.stop_requested,
        )
        if result.interrupted:
            LOGGER.info("Command interrupted during shutdown: exit=%s", result.returncode)
            return StepOutcome.STOPPED
        if result.returncode == 0 and not result.timed_out:
            if window is not None:
                mark_triggered(self.state, window.resets_at)
            else:
                assert schedule_date is not None
                mark_schedule_triggered(self.state, schedule_date)
            self._persist()
            LOGGER.info("Command completed: exit=0")
            return StepOutcome.SUCCESS if self.config.once else StepOutcome.CONTINUE

        if result.timed_out:
            LOGGER.error("Command timed out after %.1f seconds", self.config.command_timeout)
        elif result.error is not None:
            LOGGER.error("Command could not be started: %s", result.error)
        else:
            LOGGER.error("Command failed: exit=%s", result.returncode)

        if self.config.once and self.state.attempts > self.config.max_retries:
            return StepOutcome.FAILURE
        return StepOutcome.CONTINUE

    def _step_window(self) -> StepOutcome:
        window = self.detector()
        if window is not None:
            last_triggered = self.state.last_triggered_reset
            already_triggered = window.resets_at == last_triggered
            if not already_triggered and observe_reset(self.state, window.resets_at):
                self._persist()

        decision = decide_next(
            window,
            self.state,
            now=self.clock.now(),
            grace_seconds=self.config.grace_seconds,
            poll_seconds=self.config.poll_seconds,
            max_retries=self.config.max_retries,
            retry_delay_seconds=self.config.retry_delay_seconds,
        )
        self._log_phase(decision, window)
        if self.stop_requested:
            return StepOutcome.STOPPED
        if decision.phase == Phase.RETRIES_EXHAUSTED and self.config.once:
            return StepOutcome.FAILURE
        if decision.execute:
            if window is None:  # Defensive: decide_next never creates this combination.
                raise RuntimeError("execute decision without window metadata")
            return self._execute(window=window)
        self.clock.sleep(decision.sleep_seconds)
        return StepOutcome.STOPPED if self.stop_requested else StepOutcome.CONTINUE

    def _step_daily(self) -> StepOutcome:
        if self.daily_time is None:
            raise RuntimeError("daily scheduling requested without a daily time")
        now = datetime.fromtimestamp(self.clock.now()).astimezone()
        decision = decide_daily(
            self.state,
            now=now,
            daily_at=self.daily_time,
            poll_seconds=self.config.poll_seconds,
            max_retries=self.config.max_retries,
            retry_delay_seconds=self.config.retry_delay_seconds,
        )
        self._log_phase(decision, None)
        if self.stop_requested:
            return StepOutcome.STOPPED
        if decision.phase == Phase.RETRIES_EXHAUSTED and self.config.once:
            return StepOutcome.FAILURE
        if decision.execute:
            if decision.schedule_date is None:
                raise RuntimeError("daily execute decision without a schedule date")
            return self._execute(schedule_date=decision.schedule_date)
        self.clock.sleep(decision.sleep_seconds)
        return StepOutcome.STOPPED if self.stop_requested else StepOutcome.CONTINUE

    def step(self) -> StepOutcome:
        """Perform one state-machine step; exposed for deterministic tests."""
        if self.stop_requested:
            return StepOutcome.STOPPED
        if self.daily_time is not None:
            return self._step_daily()
        return self._step_window()

    def run(self, *, install_signal_handlers: bool = True) -> int:
        """Run until stopped, one successful --once trigger, or terminal failure."""
        previous_handlers: dict[
            signal.Signals, Callable[[int, FrameType | None], object] | int | None
        ] = {}
        if install_signal_handlers:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
        try:
            while True:
                outcome = self.step()
                if outcome == StepOutcome.SUCCESS:
                    if self.config.once:
                        return 0
                elif outcome == StepOutcome.FAILURE:
                    return 1
                elif outcome == StepOutcome.STOPPED:
                    return 0
        except OSError as exc:
            LOGGER.error("Keeper I/O failure: %s", exc)
            return 1
        finally:
            if install_signal_handlers:
                for sig, handler in previous_handlers.items():
                    if handler is not None:
                        signal.signal(sig, handler)


def run_service(config: KeeperConfig) -> int:
    """Validate runtime paths and launch the service."""
    if not config.workdir.is_dir():
        LOGGER.error("Working directory does not exist or is not a directory: %s", config.workdir)
        return 2
    LOGGER.info("Codex Window Keeper started")
    return KeeperService(config).run()
