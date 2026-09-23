from __future__ import annotations

import json
import shlex
from pathlib import Path

from codex_window_keeper.cli import main
from codex_window_keeper.state import load_state


def _expired_session(codex_home: Path, reset: int = 100) -> None:
    session = codex_home / "sessions" / "2026" / "09" / "21" / "integration.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 10.0,
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


def test_cli_custom_command_once_end_to_end(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    state_file = tmp_path / "state.json"
    marker = tmp_path / "marker"
    _expired_session(codex_home)

    result = main(
        [
            "--codex-home",
            str(codex_home),
            "--command",
            f"touch {shlex.quote(str(marker))}",
            "--workdir",
            str(tmp_path),
            "--state-file",
            str(state_file),
            "--grace-seconds",
            "0",
            "--once",
        ]
    )

    assert result == 0
    assert marker.exists()
    assert load_state(state_file).last_triggered_reset == 100


def test_cli_dry_run_once_has_no_side_effects(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    state_file = tmp_path / "state.json"
    marker = tmp_path / "must-not-exist"
    _expired_session(codex_home)

    result = main(
        [
            "--codex-home",
            str(codex_home),
            "--command",
            f"touch {shlex.quote(str(marker))}",
            "--workdir",
            str(tmp_path),
            "--state-file",
            str(state_file),
            "--grace-seconds",
            "0",
            "--dry-run",
            "--once",
        ]
    )

    assert result == 0
    assert not marker.exists()
    assert not state_file.exists()


def test_cli_daily_schedule_executes_once_without_session_data(tmp_path: Path) -> None:
    state_file = tmp_path / "daily-state.json"
    marker = tmp_path / "daily-marker"

    result = main(
        [
            "--daily-at",
            "00:00",
            "--command",
            f"touch {shlex.quote(str(marker))}",
            "--workdir",
            str(tmp_path),
            "--state-file",
            str(state_file),
            "--once",
        ]
    )

    assert result == 0
    assert marker.exists()
    assert load_state(state_file).last_scheduled_date is not None
