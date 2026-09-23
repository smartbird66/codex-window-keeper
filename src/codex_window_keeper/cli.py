"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timedelta

from . import __version__
from .config import MODES, ConfigError, KeeperConfig, load_config
from .detector import detect_latest_window
from .state import load_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-window-keeper",
        description=(
            "Execute a configured Codex command after the actual 5-hour reset or once daily "
            "at a configured local time."
        ),
    )
    parser.add_argument("--mode", choices=MODES, default=None)
    parser.add_argument("--prompt", default=None, help="Prompt for wake or resume modes")
    parser.add_argument("--session-id", default=None, help="Session UUID/name for resume mode")
    parser.add_argument(
        "--command", default=None, help="Trusted shell command (implies command mode)"
    )
    parser.add_argument("--workdir", default=None, help="Command working directory")
    parser.add_argument("--codex-home", default=None, help="Codex data directory")
    parser.add_argument(
        "--daily-at",
        "--wake-at",
        dest="daily_at",
        default=None,
        metavar="HH:MM",
        help="Run once daily at this local time (wake-at is an alias)",
    )
    parser.add_argument("--grace-seconds", type=float, default=None)
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--max-session-files", type=int, default=None)
    parser.add_argument("--tail-bytes", type=int, default=None)
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument("--retry-delay-seconds", type=float, default=None)
    parser.add_argument("--command-timeout", type=float, default=None, help="Timeout in seconds")
    parser.add_argument("--state-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--once", action="store_true", default=None)
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument("--status", action="store_true", default=None)
    parser.add_argument("--config", default=None, help="TOML config file")
    parser.add_argument("-v", "--verbose", action="count", default=None)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def print_status(config: KeeperConfig, *, now: float | None = None) -> int:
    """Print detected window metadata without exposing session contents."""
    current_time = time.time() if now is None else now
    keeper_state = load_state(config.state_file)
    from .service import Phase, daily_target, decide_daily, decide_next, parse_daily_time

    window = detect_latest_window(
        config.codex_home,
        max_files=config.max_session_files,
        tail_bytes=config.tail_bytes,
    )
    print("Codex 5h Window\n")
    print("CODEX_HOME:")
    print(f"  {config.codex_home}\n")
    if config.daily_at is not None:
        local_now = datetime.fromtimestamp(current_time).astimezone()
        daily_at = parse_daily_time(config.daily_at)
        daily_decision = decide_daily(
            keeper_state,
            now=local_now,
            daily_at=daily_at,
            poll_seconds=config.poll_seconds,
            max_retries=config.max_retries,
            retry_delay_seconds=config.retry_delay_seconds,
        )
        target_date = local_now.date()
        if (
            keeper_state.last_scheduled_date == target_date.isoformat()
            or daily_decision.phase == Phase.RETRIES_EXHAUSTED
        ):
            target_date += timedelta(days=1)
        next_action = daily_target(local_now, daily_at, target_date)
        if daily_decision.phase == Phase.EXECUTE:
            next_action = local_now
        elif daily_decision.phase == Phase.RETRY and keeper_state.last_attempt is not None:
            next_action = datetime.fromtimestamp(
                keeper_state.last_attempt + config.retry_delay_seconds
            ).astimezone()

        daily_state = (
            "READY_TO_TRIGGER"
            if daily_decision.phase == Phase.EXECUTE
            else daily_decision.phase.value
        )
        print("Daily schedule:")
        print(f"  {config.daily_at} local time\n")
        print("Next daily action:")
        print(f"  {next_action.isoformat(sep=' ', timespec='seconds')}\n")
        print("Daily state:")
        print(f"  {daily_state}\n")

    if window is None:
        print("No valid 300-minute Codex usage window was detected.\n")
        print("Possible causes:")
        print("- Codex has not been used recently.")
        print("- CODEX_HOME is incorrect.")
        print("- Codex changed its local session format.\n")
        print("Run:")
        print("    codex-window-keeper --status --verbose")
        return 0 if config.daily_at is not None else 1

    reset_time = datetime.fromtimestamp(window.resets_at).astimezone()

    decision = decide_next(
        window,
        keeper_state,
        now=current_time,
        grace_seconds=config.grace_seconds,
        poll_seconds=config.poll_seconds,
        max_retries=config.max_retries,
        retry_delay_seconds=config.retry_delay_seconds,
    )
    state = "READY_TO_TRIGGER" if decision.phase == Phase.EXECUTE else decision.phase.value

    if window.used_percent is None:
        used = "unknown"
    elif window.used_percent.is_integer():
        used = f"{int(window.used_percent)}%"
    else:
        used = f"{window.used_percent:g}%"

    print("Latest session:")
    print(f"  {window.source_file}\n")
    print("Window:")
    print(f"  {window.window_minutes} minutes\n")
    print("Used:")
    print(f"  {used}\n")
    print("Reset timestamp:")
    print(f"  {window.resets_at}\n")
    print("Reset local time:")
    print(f"  {reset_time.isoformat(sep=' ', timespec='seconds')}\n")
    print("Remaining:")
    print(f"  {_duration(window.resets_at - current_time)}\n")
    print("State:")
    print(f"  {state}")
    return 0


def _configure_logging(verbose: int) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments)
    except ConfigError as exc:
        parser.error(str(exc))

    _configure_logging(config.verbose)
    if config.status:
        return print_status(config)

    # Imported lazily so status remains an independent detector diagnostic.
    from .service import run_service

    return run_service(config)
