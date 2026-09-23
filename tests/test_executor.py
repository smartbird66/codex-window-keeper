from __future__ import annotations

import shlex
import time
from dataclasses import replace
from pathlib import Path

from codex_window_keeper.config import KeeperConfig
from codex_window_keeper.executor import CommandExecutor, build_command


def _config(tmp_path: Path, **changes: object) -> KeeperConfig:
    base = KeeperConfig(
        mode="wake",
        codex_home=tmp_path / ".codex",
        workdir=tmp_path,
        prompt="hello",
        session_id=None,
        command=None,
        grace_seconds=10,
        poll_seconds=20,
        max_session_files=50,
        tail_bytes=2 * 1024 * 1024,
        max_retries=3,
        retry_delay_seconds=300,
        command_timeout=300,
        state_file=tmp_path / "state.json",
        config_file=tmp_path / "config.toml",
        dry_run=False,
        once=False,
        status=False,
        verbose=0,
    )
    return replace(base, **changes)


def test_builds_wake_command_without_shell(tmp_path: Path) -> None:
    command = build_command(_config(tmp_path))

    assert command.argv == ("codex", "exec", "hello")
    assert command.display == 'codex exec "hello"'
    assert command.log_display == command.display


def test_builds_resume_last_command_for_current_workdir(tmp_path: Path) -> None:
    command = build_command(_config(tmp_path, mode="resume-last", prompt="continue"))

    assert command.argv == ("codex", "exec", "resume", "--last", "continue")
    assert "continue" not in command.display


def test_builds_resume_session_command(tmp_path: Path) -> None:
    command = build_command(
        _config(tmp_path, mode="resume", session_id="session-123", prompt="continue")
    )

    assert command.argv == ("codex", "exec", "resume", "session-123", "continue")
    assert "continue" not in command.display


def test_custom_command_uses_explicit_shell_mode(tmp_path: Path) -> None:
    command = build_command(_config(tmp_path, mode="command", command="printf ok"))

    assert command.argv == ("/bin/sh", "-c", "printf ok")
    assert command.display == "printf ok"
    assert command.log_display == "<custom command redacted>"


def test_executor_reports_success_and_failure(tmp_path: Path) -> None:
    executor = CommandExecutor()
    success = executor.run(
        build_command(_config(tmp_path, mode="command", command="/bin/true")),
        workdir=tmp_path,
        timeout=1,
    )
    failure = executor.run(
        build_command(_config(tmp_path, mode="command", command="/bin/false")),
        workdir=tmp_path,
        timeout=1,
    )

    assert success.returncode == 0
    assert not success.timed_out
    assert failure.returncode != 0
    assert not failure.timed_out


def test_executor_uses_configured_working_directory(tmp_path: Path) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    quoted = shlex.quote(str(workdir))
    command = build_command(
        _config(tmp_path, mode="command", command=f'test "$PWD" = {quoted}')
    )

    result = CommandExecutor().run(command, workdir=workdir, timeout=1)

    assert result.returncode == 0


def test_executor_times_out_and_terminates_process_group(tmp_path: Path) -> None:
    command = build_command(_config(tmp_path, mode="command", command="sleep 10"))
    executor = CommandExecutor(poll_interval=0.01, termination_grace=0.05)

    result = executor.run(command, workdir=tmp_path, timeout=0.05)

    assert result.timed_out
    assert result.returncode != 0


def test_executor_honors_shutdown_request(tmp_path: Path) -> None:
    command = build_command(_config(tmp_path, mode="command", command="sleep 10"))
    executor = CommandExecutor(poll_interval=0.01, termination_grace=0.05)
    start = time.monotonic()

    result = executor.run(
        command,
        workdir=tmp_path,
        timeout=10,
        stop_requested=lambda: time.monotonic() - start >= 0.05,
    )

    assert result.interrupted
    assert not result.timed_out
    assert result.returncode != 0


def test_spawn_error_is_returned_as_command_failure(tmp_path: Path) -> None:
    command = build_command(_config(tmp_path, mode="command", command="/bin/true"))

    result = CommandExecutor().run(command, workdir=tmp_path / "missing", timeout=1)

    assert result.returncode == 127
    assert result.error is not None
