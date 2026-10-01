# codex-window-keeper

`codex-window-keeper` is a small Linux service that runs a Codex CLI command after the real
five-hour usage window resets, or once per day at a configured local time.

> This tool does not bypass or reset Codex rate limits. It sends ordinary Codex CLI requests and
> follows the server-provided `resets_at` value.

## Features

- Reads only rate-limit metadata from local Codex session JSONL files.
- Detects the primary window with `window_minutes == 300`.
- Never estimates the next window as `old reset + 5 hours`.
- Persists state to prevent duplicate execution after restarts.
- Supports a daily local-time schedule, retries, timeouts, grace periods, and dry runs.
- Runs a small `codex exec "hello"` request by default; resume and shell modes require explicit
  configuration.

## Requirements

- Linux
- Python 3.11 or newer
- Codex CLI available on `PATH`
- A trusted Git repository for commands that use `codex exec`

## Installation

Clone the repository and install it with [pipx](https://pipx.pypa.io/):

```bash
git clone https://github.com/smartbird66/codex-window-keeper.git
cd codex-window-keeper
pipx install --python python3.11 .
codex-window-keeper --version
```

Choose any installed Python version that satisfies `>=3.11`. An active virtual environment does
not necessarily change the interpreter selected by pipx.

For development, install an editable environment instead:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## Quick start

First inspect the detected window. This is read-only and does not call the Codex API:

```bash
codex-window-keeper --status
```

Codex CLI normally requires a trusted Git working directory. For wake-only requests, a dedicated
empty repository keeps the task isolated from real projects:

```bash
mkdir -p "$HOME/.local/share/codex-window-keeper/wake-workspace"
git -C "$HOME/.local/share/codex-window-keeper/wake-workspace" init
```

Test the next reset-driven action without executing it:

```bash
codex-window-keeper \
  --workdir "$HOME/.local/share/codex-window-keeper/wake-workspace" \
  --dry-run \
  --once
```

Then run continuously:

```bash
codex-window-keeper \
  --workdir "$HOME/.local/share/codex-window-keeper/wake-workspace"
```

After a successful trigger, the service waits until Codex records a different real `resets_at`
value before it can trigger again.

## Daily wake schedule

To send the default `hello` request every day at 06:00 local time:

```bash
codex-window-keeper \
  --daily-at 06:00 \
  --workdir "$HOME/.local/share/codex-window-keeper/wake-workspace"
```

`--wake-at` is an alias for `--daily-at`. Daily scheduling replaces reset-driven triggering; it
does not force a new five-hour window if an existing window is still active. If the service starts
after the scheduled time, it catches up once on the same local calendar day.

Use a separate state file for every independent keeper instance:

```bash
codex-window-keeper \
  --daily-at 06:00 \
  --workdir "$HOME/.local/share/codex-window-keeper/wake-workspace" \
  --state-file "$HOME/.local/state/codex-window-keeper/wake-0600.json"
```

Retry state is scoped to the reset timestamp or local date. Changing `--daily-at` does not clear
failed attempts already recorded for that day; use a fresh test state file when testing another
time on the same day.

For a simple background run without systemd:

```bash
mkdir -p logs
nohup codex-window-keeper \
  --daily-at 06:00 \
  --workdir "$HOME/.local/share/codex-window-keeper/wake-workspace" \
  > logs/codex-window-keeper.log 2>&1 &
```

## Execution modes

| Mode | Command |
| --- | --- |
| `wake` (default) | `codex exec PROMPT` |
| `resume-last` | `codex exec resume --last PROMPT` |
| `resume` / `resume-session` | `codex exec resume SESSION_ID PROMPT` |
| `command` / `custom-command` | Trusted shell command through `/bin/sh -c` |

Examples:

```bash
# Resume the latest session associated with this project.
codex-window-keeper \
  --mode resume-last \
  --workdir "$HOME/project" \
  --prompt "Continue the previous task"

# Resume one explicit session.
codex-window-keeper \
  --mode resume-session \
  --session-id 019xxxxx-xxxx-xxxx \
  --workdir "$HOME/project" \
  --prompt "Continue the previous task"

# Run a trusted local script.
codex-window-keeper \
  --command './scripts/run-codex-task.sh' \
  --workdir "$HOME/project"
```

Prompts are redacted from normal keeper logs. Custom commands are shown during `--dry-run` but
redacted during real execution. Only configure shell commands you trust.

## Configuration

The default configuration file is `~/.config/codex-window-keeper/config.toml`. Copy and edit the
example:

```bash
mkdir -p "$HOME/.config/codex-window-keeper"
cp config.example.toml "$HOME/.config/codex-window-keeper/config.toml"
```

Configuration precedence is CLI arguments, environment variables, TOML, then built-in defaults.
Run `codex-window-keeper --help` for all CLI options.

The default state file is:

```text
~/.local/state/codex-window-keeper/state.json
```

`max_retries = 3` means one initial attempt plus at most three retries. The default retry delay is
300 seconds. The default timeout is 300 seconds for wake mode and 1,800 seconds for other modes.

## systemd user service

After validating `--status`, `--dry-run`, and your configuration:

```bash
mkdir -p "$HOME/.config/systemd/user"
cp systemd/codex-window-keeper.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now codex-window-keeper.service
```

Inspect the service with:

```bash
systemctl --user status codex-window-keeper.service
journalctl --user -u codex-window-keeper.service -f
```

The systemd user manager may have a different `PATH` from an interactive shell. Ensure both
`codex-window-keeper` and `codex` are available to the service. See
[Troubleshooting](docs/troubleshooting.md) for details.

## Codex data compatibility

The detector was validated against `codex-cli 0.155.1` session events shaped like:

```json
{
  "rate_limits": {
    "primary": {
      "used_percent": 54.0,
      "window_minutes": 300,
      "resets_at": 1789988781
    }
  }
}
```

The scanner tolerates wrapper objects and extra fields, but Codex session JSONL is an internal
format and may change. It never returns or logs complete session records, prompts, or code. Do not
attach full session files to bug reports.

## Development

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src
```

Tests use synthetic fixtures and local commands; they do not send Codex requests.

See [Architecture](docs/architecture.md) and [Troubleshooting](docs/troubleshooting.md) for more
detail. This project is licensed under the [MIT License](LICENSE).
