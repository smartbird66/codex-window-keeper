"""Configuration loading with CLI > environment > TOML > defaults precedence."""

from __future__ import annotations

import argparse
import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODES = ("wake", "resume-last", "resume", "resume-session", "command", "custom-command")
_MODE_ALIASES = {"resume-session": "resume", "custom-command": "command"}


class ConfigError(ValueError):
    """Raised when keeper configuration is invalid."""


@dataclass(frozen=True, slots=True)
class KeeperConfig:
    """Validated effective configuration."""

    mode: str
    codex_home: Path
    workdir: Path
    prompt: str
    session_id: str | None
    command: str | None
    grace_seconds: float
    poll_seconds: float
    max_session_files: int
    tail_bytes: int
    max_retries: int
    retry_delay_seconds: float
    command_timeout: float
    state_file: Path
    config_file: Path
    dry_run: bool
    once: bool
    status: bool
    verbose: int
    daily_at: str | None = None


_CONFIG_KEYS = {
    "mode",
    "codex_home",
    "workdir",
    "prompt",
    "session_id",
    "command",
    "grace_seconds",
    "poll_seconds",
    "max_session_files",
    "tail_bytes",
    "max_retries",
    "retry_delay_seconds",
    "command_timeout",
    "state_file",
    "daily_at",
}

_ENV_KEYS = {
    "CODEX_HOME": "codex_home",
    "CODEX_KEEPER_MODE": "mode",
    "CODEX_KEEPER_COMMAND": "command",
    "CODEX_KEEPER_WORKDIR": "workdir",
    "CODEX_KEEPER_PROMPT": "prompt",
    "CODEX_KEEPER_SESSION_ID": "session_id",
    "CODEX_KEEPER_GRACE_SECONDS": "grace_seconds",
    "CODEX_KEEPER_POLL_SECONDS": "poll_seconds",
    "CODEX_KEEPER_MAX_SESSION_FILES": "max_session_files",
    "CODEX_KEEPER_TAIL_BYTES": "tail_bytes",
    "CODEX_KEEPER_MAX_RETRIES": "max_retries",
    "CODEX_KEEPER_RETRY_DELAY": "retry_delay_seconds",
    "CODEX_KEEPER_COMMAND_TIMEOUT": "command_timeout",
    "CODEX_KEEPER_STATE_FILE": "state_file",
    "CODEX_KEEPER_DAILY_AT": "daily_at",
}


def default_config_path(environment: Mapping[str, str] = os.environ) -> Path:
    fallback = Path(environment.get("HOME", str(Path.home()))) / ".config"
    base = Path(environment.get("XDG_CONFIG_HOME", str(fallback))).expanduser()
    return base / "codex-window-keeper" / "config.toml"


def default_state_path(environment: Mapping[str, str] = os.environ) -> Path:
    fallback = Path(environment.get("HOME", str(Path.home()))) / ".local" / "state"
    base = Path(environment.get("XDG_STATE_HOME", str(fallback))).expanduser()
    return base / "codex-window-keeper" / "state.json"


def _read_toml(path: Path, *, required: bool) -> dict[str, Any]:
    try:
        with path.open("rb") as config_file:
            parsed = tomllib.load(config_file)
    except FileNotFoundError:
        if required:
            raise ConfigError(f"Configuration file does not exist: {path}") from None
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Could not read configuration file {path}: {exc}") from exc

    unknown = set(parsed) - _CONFIG_KEYS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ConfigError(f"Unknown configuration key(s): {names}")
    return parsed


def _float_value(name: str, value: object, *, minimum: float) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a number")
    if not isinstance(value, (int, float, str)):
        raise ConfigError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not math.isfinite(result) or result < minimum:
        raise ConfigError(f"{name} must be at least {minimum:g}")
    return result


def _int_value(name: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ConfigError(f"{name} must be an integer")
        result = int(value)
    elif isinstance(value, str):
        try:
            result = int(value)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer") from exc
        if str(result) != value.strip():
            raise ConfigError(f"{name} must be an integer")
    else:
        raise ConfigError(f"{name} must be an integer")
    if result < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return result


def _string(name: str, value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def _daily_time(value: object) -> str:
    """Validate and normalize a local wall-clock time as HH:MM."""
    raw = _string("daily_at", value).strip()
    parts = raw.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ConfigError("daily_at must use HH:MM in 24-hour local time")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigError("daily_at must use HH:MM in 24-hour local time")
    return f"{hour:02d}:{minute:02d}"


def load_config(
    arguments: argparse.Namespace,
    environment: Mapping[str, str] = os.environ,
) -> KeeperConfig:
    """Merge and validate all configuration sources."""
    explicit_config = arguments.config is not None
    config_file = (
        Path(arguments.config).expanduser() if explicit_config else default_config_path(environment)
    )
    file_values = _read_toml(config_file, required=explicit_config)
    env_values = {
        target: environment[source]
        for source, target in _ENV_KEYS.items()
        if source in environment
    }

    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    values: dict[str, object] = {
        "mode": "wake",
        "codex_home": str(home / ".codex"),
        "workdir": str(home),
        "prompt": "hello",
        "session_id": None,
        "command": None,
        "grace_seconds": 10,
        "poll_seconds": 20,
        "max_session_files": 50,
        "tail_bytes": 2 * 1024 * 1024,
        "max_retries": 3,
        "retry_delay_seconds": 300,
        "command_timeout": None,
        "state_file": str(default_state_path(environment)),
        "daily_at": None,
    }
    values.update(file_values)
    values.update(env_values)

    for field in _CONFIG_KEYS:
        cli_value = getattr(arguments, field, None)
        if cli_value is not None:
            values[field] = cli_value

    mode_was_set = (
        "mode" in file_values
        or "mode" in env_values
        or getattr(arguments, "mode", None) is not None
    )
    command_was_set = (
        "command" in file_values
        or "command" in env_values
        or getattr(arguments, "command", None) is not None
    )
    if command_was_set and not mode_was_set:
        values["mode"] = "command"

    mode = _string("mode", values["mode"])
    mode = _MODE_ALIASES.get(mode, mode)
    if mode not in {"wake", "resume-last", "resume", "command"}:
        raise ConfigError(f"Unsupported mode: {mode}")

    prompt = _string("prompt", values["prompt"], allow_empty=True)
    command_value = values["command"]
    command = None if command_value is None else _string("command", command_value)
    session_value = values["session_id"]
    session_id = None if session_value is None else _string("session_id", session_value)

    if not arguments.status:
        if mode == "command" and command is None:
            raise ConfigError("command mode requires --command or CODEX_KEEPER_COMMAND")
        if mode == "resume" and session_id is None:
            raise ConfigError("resume mode requires --session-id or CODEX_KEEPER_SESSION_ID")

    timeout_value = values["command_timeout"]
    if timeout_value is None:
        timeout_value = 300 if mode == "wake" else 1800

    daily_value = values["daily_at"]
    daily_at = None if daily_value is None else _daily_time(daily_value)

    return KeeperConfig(
        mode=mode,
        codex_home=Path(_string("codex_home", values["codex_home"])).expanduser(),
        workdir=Path(_string("workdir", values["workdir"])).expanduser(),
        prompt=prompt,
        session_id=session_id,
        command=command,
        grace_seconds=_float_value("grace_seconds", values["grace_seconds"], minimum=0),
        poll_seconds=_float_value("poll_seconds", values["poll_seconds"], minimum=0.1),
        max_session_files=_int_value(
            "max_session_files", values["max_session_files"], minimum=1
        ),
        tail_bytes=_int_value("tail_bytes", values["tail_bytes"], minimum=1),
        max_retries=_int_value("max_retries", values["max_retries"], minimum=0),
        retry_delay_seconds=_float_value(
            "retry_delay_seconds", values["retry_delay_seconds"], minimum=1
        ),
        command_timeout=_float_value("command_timeout", timeout_value, minimum=0.1),
        state_file=Path(_string("state_file", values["state_file"])).expanduser(),
        config_file=config_file,
        dry_run=bool(arguments.dry_run),
        once=bool(arguments.once),
        status=bool(arguments.status),
        verbose=int(arguments.verbose or 0),
        daily_at=daily_at,
    )
