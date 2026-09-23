from __future__ import annotations

from pathlib import Path

import pytest

from codex_window_keeper.cli import build_parser
from codex_window_keeper.config import ConfigError, load_config


def _arguments(*values: str):  # type: ignore[no-untyped-def]
    return build_parser().parse_args(values)


def test_defaults_are_conservative(tmp_path: Path) -> None:
    config = load_config(_arguments(), {"HOME": str(tmp_path)})

    assert config.mode == "wake"
    assert config.prompt == "hello"
    assert config.codex_home == tmp_path / ".codex"
    assert config.workdir == tmp_path
    assert config.grace_seconds == 10
    assert config.command_timeout == 300
    assert config.state_file == tmp_path / ".local/state/codex-window-keeper/state.json"


def test_cli_overrides_environment_and_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "keeper.toml"
    config_path.write_text(
        'mode = "wake"\nprompt = "from file"\npoll_seconds = 60\n', encoding="utf-8"
    )
    environment = {
        "HOME": str(tmp_path),
        "CODEX_KEEPER_PROMPT": "from env",
        "CODEX_KEEPER_POLL_SECONDS": "40",
    }

    config = load_config(
        _arguments(
            "--config",
            str(config_path),
            "--prompt",
            "from cli",
            "--poll-seconds",
            "20",
        ),
        environment,
    )

    assert config.prompt == "from cli"
    assert config.poll_seconds == 20


def test_command_without_mode_implies_custom_command_mode(tmp_path: Path) -> None:
    config = load_config(
        _arguments("--command", "echo ok"),
        {"HOME": str(tmp_path)},
    )

    assert config.mode == "command"
    assert config.command == "echo ok"
    assert config.command_timeout == 1800


@pytest.mark.parametrize(
    ("provided", "canonical"),
    [("resume-session", "resume"), ("custom-command", "command")],
)
def test_mode_aliases(tmp_path: Path, provided: str, canonical: str) -> None:
    extra = ["--session-id", "abc"] if canonical == "resume" else ["--command", "true"]

    config = load_config(
        _arguments("--mode", provided, *extra),
        {"HOME": str(tmp_path)},
    )

    assert config.mode == canonical


def test_environment_variables_are_supported(tmp_path: Path) -> None:
    environment = {
        "HOME": str(tmp_path),
        "CODEX_HOME": str(tmp_path / "codex-data"),
        "CODEX_KEEPER_MODE": "resume-last",
        "CODEX_KEEPER_WORKDIR": str(tmp_path / "project"),
        "CODEX_KEEPER_PROMPT": "continue",
        "CODEX_KEEPER_GRACE_SECONDS": "12.5",
        "CODEX_KEEPER_POLL_SECONDS": "8",
        "CODEX_KEEPER_MAX_RETRIES": "2",
        "CODEX_KEEPER_RETRY_DELAY": "90",
        "CODEX_KEEPER_DAILY_AT": "6:05",
    }

    config = load_config(_arguments(), environment)

    assert config.codex_home == tmp_path / "codex-data"
    assert config.mode == "resume-last"
    assert config.prompt == "continue"
    assert config.grace_seconds == 12.5
    assert config.poll_seconds == 8
    assert config.max_retries == 2
    assert config.retry_delay_seconds == 90
    assert config.daily_at == "06:05"


@pytest.mark.parametrize("option", ["--daily-at", "--wake-at"])
def test_daily_time_cli_and_alias_are_normalized(tmp_path: Path, option: str) -> None:
    config = load_config(_arguments(option, "6:00"), {"HOME": str(tmp_path)})

    assert config.daily_at == "06:00"
    assert config.mode == "wake"


@pytest.mark.parametrize("value", ["", "6", "06:60", "24:00", "noon", "06:00:00"])
def test_invalid_daily_time_is_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="daily_at"):
        load_config(_arguments("--daily-at", value), {"HOME": str(tmp_path)})


def test_daily_time_can_be_loaded_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('daily_at = "06:00"\n', encoding="utf-8")

    config = load_config(
        _arguments("--config", str(path)),
        {"HOME": str(tmp_path)},
    )

    assert config.daily_at == "06:00"


def test_resume_requires_session_id_except_for_status(tmp_path: Path) -> None:
    environment = {"HOME": str(tmp_path)}

    with pytest.raises(ConfigError, match="session-id"):
        load_config(_arguments("--mode", "resume"), environment)

    config = load_config(_arguments("--mode", "resume", "--status"), environment)
    assert config.status


def test_custom_command_requires_command(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="requires --command"):
        load_config(_arguments("--mode", "command"), {"HOME": str(tmp_path)})


def test_explicit_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(
            _arguments("--config", str(tmp_path / "missing.toml")),
            {"HOME": str(tmp_path)},
        )


def test_unknown_toml_key_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("poll_secondz = 3\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="poll_secondz"):
        load_config(_arguments("--config", str(path)), {"HOME": str(tmp_path)})


@pytest.mark.parametrize(
    "option",
    [
        ("--grace-seconds", "-1"),
        ("--poll-seconds", "0"),
        ("--max-session-files", "0"),
        ("--tail-bytes", "0"),
        ("--max-retries", "-1"),
        ("--retry-delay-seconds", "0"),
        ("--command-timeout", "nan"),
    ],
)
def test_invalid_numeric_values_are_rejected(tmp_path: Path, option: tuple[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_config(_arguments(*option), {"HOME": str(tmp_path)})
