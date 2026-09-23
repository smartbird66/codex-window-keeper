"""Codex and custom-command construction and execution."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import KeeperConfig
from .models import ExecutionResult

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """An argv and its safe-to-log representation."""

    argv: tuple[str, ...]
    display: str
    log_display: str


def build_command(config: KeeperConfig) -> CommandSpec:
    """Build an argv for the configured mode without invoking a shell unnecessarily."""
    if config.mode == "wake":
        display = (
            'codex exec "hello"'
            if config.prompt == "hello"
            else "codex exec <prompt redacted>"
        )
        return CommandSpec(("codex", "exec", config.prompt), display, display)
    if config.mode == "resume-last":
        return CommandSpec(
            ("codex", "exec", "resume", "--last", config.prompt),
            "codex exec resume --last <prompt redacted>",
            "codex exec resume --last <prompt redacted>",
        )
    if config.mode == "resume":
        if config.session_id is None:
            raise ValueError("resume mode requires a session ID")
        return CommandSpec(
            ("codex", "exec", "resume", config.session_id, config.prompt),
            f"codex exec resume {config.session_id} <prompt redacted>",
            f"codex exec resume {config.session_id} <prompt redacted>",
        )
    if config.mode == "command":
        if config.command is None:
            raise ValueError("command mode requires a command")
        # Custom command is an explicitly advanced interface. /bin/sh semantics
        # are documented and no session-derived value is interpolated into it.
        return CommandSpec(
            ("/bin/sh", "-c", config.command),
            config.command,
            "<custom command redacted>",
        )
    raise ValueError(f"Unsupported execution mode: {config.mode}")


class CommandExecutor:
    """Run one command with timeout and cooperative shutdown handling."""

    def __init__(self, *, poll_interval: float = 0.1, termination_grace: float = 5.0) -> None:
        self.poll_interval = poll_interval
        self.termination_grace = termination_grace

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()

    def _stop_process(self, process: subprocess.Popen[bytes]) -> int:
        self._signal_process_group(process, signal.SIGTERM)
        try:
            return process.wait(timeout=self.termination_grace)
        except subprocess.TimeoutExpired:
            self._signal_process_group(process, signal.SIGKILL)
            return process.wait()

    def run(
        self,
        command: CommandSpec,
        *,
        workdir: Path,
        timeout: float,
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> ExecutionResult:
        """Execute a command and terminate its process group on timeout or shutdown."""
        started_at = time.time()
        started_monotonic = time.monotonic()
        try:
            process = subprocess.Popen(command.argv, cwd=workdir, start_new_session=True)
        except OSError as exc:
            return ExecutionResult(
                returncode=127,
                started_at=started_at,
                finished_at=time.time(),
                timed_out=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        while True:
            if stop_requested():
                returncode = self._stop_process(process)
                return ExecutionResult(
                    returncode=returncode,
                    started_at=started_at,
                    finished_at=time.time(),
                    timed_out=False,
                    interrupted=True,
                )

            elapsed = time.monotonic() - started_monotonic
            remaining = timeout - elapsed
            if remaining <= 0:
                returncode = self._stop_process(process)
                return ExecutionResult(
                    returncode=returncode,
                    started_at=started_at,
                    finished_at=time.time(),
                    timed_out=True,
                )

            try:
                returncode = process.wait(timeout=min(self.poll_interval, remaining))
            except subprocess.TimeoutExpired:
                continue
            return ExecutionResult(
                returncode=returncode,
                started_at=started_at,
                finished_at=time.time(),
                timed_out=False,
            )
