from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from codex_window_keeper.cli import print_status
from codex_window_keeper.config import KeeperConfig
from codex_window_keeper.state import KeeperState, save_state


def _config(tmp_path: Path) -> KeeperConfig:
    return KeeperConfig(
        mode="wake",
        codex_home=tmp_path / ".codex",
        workdir=tmp_path,
        prompt="hello",
        session_id=None,
        command=None,
        grace_seconds=10,
        poll_seconds=20,
        max_session_files=50,
        tail_bytes=4096,
        max_retries=3,
        retry_delay_seconds=300,
        command_timeout=300,
        state_file=tmp_path / "state.json",
        config_file=tmp_path / "config.toml",
        dry_run=False,
        once=False,
        status=True,
        verbose=0,
    )


def _write_window(config: KeeperConfig, reset: int, used: float = 57) -> Path:
    session = config.codex_home / "sessions" / "2026" / "09" / "21" / "test.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": used,
                            "window_minutes": 300,
                            "resets_at": reset,
                        }
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return session


def test_status_prints_detected_window(tmp_path: Path, capsys: object) -> None:
    config = _config(tmp_path)
    session = _write_window(config, reset=10_000)

    result = print_status(config, now=1_000)

    assert result == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert str(session) in output
    assert "300 minutes" in output
    assert "57%" in output
    assert "10000" in output
    assert "02:30:00" in output
    assert "WAIT_FOR_RESET" in output


def test_status_reports_ready_after_grace_period(tmp_path: Path, capsys: object) -> None:
    config = _config(tmp_path)
    _write_window(config, reset=100)

    assert print_status(config, now=111) == 0

    assert "READY_TO_TRIGGER" in capsys.readouterr().out  # type: ignore[attr-defined]


def test_status_reports_already_triggered_window(tmp_path: Path, capsys: object) -> None:
    config = _config(tmp_path)
    _write_window(config, reset=100)
    save_state(
        config.state_file,
        KeeperState(last_seen_reset=100, last_triggered_reset=100, attempts=1),
    )

    assert print_status(config, now=1_000) == 0

    assert "WAIT_NEW_RESET" in capsys.readouterr().out  # type: ignore[attr-defined]


def test_status_failure_is_safe_and_actionable(tmp_path: Path, capsys: object) -> None:
    config = _config(tmp_path)

    assert print_status(config, now=1_000) == 1

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "No valid 300-minute" in output
    assert "--status --verbose" in output


def test_status_reports_daily_schedule_without_session_data(
    tmp_path: Path, capsys: object
) -> None:
    config = replace(_config(tmp_path), daily_at="06:00")

    assert print_status(config, now=1_795_315_200) == 0

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Daily schedule:" in output
    assert "06:00 local time" in output
    assert "Daily state:" in output
    assert "No valid 300-minute" in output
