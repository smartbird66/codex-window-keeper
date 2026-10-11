# codex-window-keeper

`codex-window-keeper` is a small Linux service that helps align Codex's real five-hour usage
window with your work schedule and automatically runs a Codex CLI action at the right time.

If you use Codex with ChatGPT Plus, the 5-hour limit can be annoying when a bunch of tasks land at
the same time. But paying more for 5x usage may be unnecessary when that is far more than you need.
Sometimes you do not need a bigger quota; you just want the window to reset at a better time.

## Why this project exists

When a five-hour window starts with your first request in the morning, its later reset points may
fall near the end of the workday or after it. That can leave fewer opportunities to continue
quota-heavy work during working hours.

This project can send a lightweight Codex request at a configured local time, such as 06:00, so a
new window can start before work when no existing window is still active. It can also watch the
real server-provided reset timestamp and run a configured action after that reset. The result is a
more useful schedule without guessing or changing Codex's rate-limit rules.

![Align the five-hour Codex window with your workday](<docs/Align 5h window with your workdays.png>)

> This tool does not bypass or reset Codex rate limits. It sends ordinary Codex CLI requests and
> follows the server-provided `resets_at` value.

## What it does

- Runs a small `codex exec "hello"` request once per day at a configured local time.
- Alternatively, watches the real five-hour `resets_at` timestamp and runs after the reset.
- Supports wake, resume, and trusted custom-command modes.
- Persists state to prevent duplicate execution after restarts.
- Provides retries, timeouts, grace periods, status inspection, and dry runs.
- Reads only rate-limit metadata from local Codex session JSONL files.
- Detects the primary window with `window_minutes == 300` and never estimates the next reset as
  `old reset + 5 hours`.

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

Codex CLI normally requires a trusted Git working directory. From the cloned project root, create
an ignored local workspace for wake-only requests and keeper state:

```bash
mkdir -p workspace/workdir workspace/state
```

The project repository provides the trusted Git context, while `workspace/` keeps runtime files
out of version control. The command working directory and keeper state are separated so the Codex
request does not run directly beside its scheduler state.

Test the next reset-driven action without executing it:

```bash
codex-window-keeper \
  --workdir "$PWD/workspace/workdir" \
  --state-file "$PWD/workspace/state/reset-wake.json" \
  --dry-run \
  --once
```

Then run continuously:

```bash
codex-window-keeper \
  --workdir "$PWD/workspace/workdir" \
  --state-file "$PWD/workspace/state/reset-wake.json"
```

After a successful trigger, the service waits until Codex records a different real `resets_at`
value before it can trigger again.

## Daily wake schedule

To send the default `hello` request every day at 06:00 local time:

```bash
codex-window-keeper \
  --daily-at 06:00 \
  --workdir "$PWD/workspace/workdir" \
  --state-file "$PWD/workspace/state/wake-0600.json"
```

`--wake-at` is an alias for `--daily-at`. Daily scheduling replaces reset-driven triggering; it
does not force a new five-hour window if an existing window is still active. If the service starts
after the scheduled time, it catches up once on the same local calendar day.

### State files and multiple tasks

The state file records completed triggers, failed attempts, and retry timing. Always assign a
different state file to every independently running keeper task. Sharing one state file causes a
failure or successful trigger from one task to affect the others.

Use a descriptive filename for each task:

```bash
codex-window-keeper \
  --daily-at 06:00 \
  --workdir "$PWD/workspace/workdir" \
  --state-file "$PWD/workspace/state/wake-0600.json"
```

For example, another task should use a name such as `workspace/state/project-sync.json`, not
`wake-0600.json`. The parent directory and state file are created automatically. Retry state is
scoped to the reset timestamp or local date, so changing `--daily-at` does not clear failed
attempts already recorded for that day. Use a fresh state file when testing another schedule on
the same day.

For a simple background run without systemd:

```bash
mkdir -p logs workspace/workdir workspace/state
nohup codex-window-keeper \
  --daily-at 06:00 \
  --workdir "$PWD/workspace/workdir" \
  --state-file "$PWD/workspace/state/wake-0600.json" \
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

`--state-file` overrides this path. The quick-start examples keep named state files under the
project's ignored `workspace/state/` directory for visibility and task isolation. For a permanent
installation that should survive removal of the source checkout, keep state under
`~/.local/state/codex-window-keeper/` instead. Whether project-local or user-local, never share one
state file between independent tasks.

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
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src
```

See [Architecture](docs/architecture.md) and [Troubleshooting](docs/troubleshooting.md) for more
detail. This project is licensed under the [MIT License](LICENSE).
