# Troubleshooting

## No valid 300-minute window detected

Run:

```bash
codex-window-keeper --status --verbose
```

Then check, without printing session contents:

```bash
printf '%s\n' "${CODEX_HOME:-$HOME/.codex}"
find "${CODEX_HOME:-$HOME/.codex}/sessions" -type f -name '*.jsonl' | tail
```

Likely causes are no recent Codex activity, a wrong `CODEX_HOME`, unreadable files, a tail limit too
small for unusual logs, or a Codex internal schema change. Increase `max_session_files` or
`tail_bytes` cautiously. Session files can contain prompts, code, project paths, and user data; do
not paste complete lines into bug reports. Report only redacted key paths and scalar types.

## WAIT_NEW_RESET lasts longer than expected

This is deliberate. After a successful trigger, the service refuses to calculate `old reset + 5h`.
It waits until Codex persists a different `resets_at`. Check Codex output in the journal and rerun
`--status`. A failed/blocked request may not create a new window.

## Daily wake did not produce the expected five-hour reset

Check the server timezone and schedule:

```bash
date
codex-window-keeper --status
```

`daily_at` is interpreted in server local time, not the SSH client's timezone. The command runs at
the first poll on or after that time. A scheduled `hello` cannot force-reset a Codex window that is
still active; inspect the real `resets_at` shown by `--status`. If the service was offline at the
configured time, it catches up once when started later that same local day.

## RETRIES_EXHAUSTED

The initial command and all configured retries failed. Inspect journald for exit code, timeout, an
invalid working directory, authentication, network failure, or server rate limiting. The keeper
will not hammer the service. Fix the cause, then either wait for Codex activity to record a new
window or deliberately edit/remove only the keeper's own state file while the keeper is stopped.
Never edit a Codex session to force a trigger.

Daily retry accounting is keyed by local calendar date. Changing `daily_at` on the same day does
not clear attempts from an earlier schedule. For isolated testing or multiple keeper instances,
assign a separate `state_file` to each instance.

## Codex reports an untrusted directory

`codex exec` normally requires its working directory to be inside a trusted Git repository. The
keeper's default working directory is the user's home directory, not necessarily the shell's
current directory. Set `workdir` in TOML or pass it explicitly:

```bash
codex-window-keeper --workdir /path/to/trusted/git/repository
```

For wake-only requests, an empty dedicated Git repository is sufficient. Avoid
`--skip-git-repo-check` unless you understand and accept the trust implications.

## resume-last chooses no session or the wrong session

Codex CLI 0.155.1 filters `--last` using the current working directory. Set `workdir` to the same
project directory used by the target Codex session, or use `mode = "resume"` with an explicit
`session_id`.

## Service works in a shell but not under systemd

Inspect:

```bash
systemctl --user status codex-window-keeper.service
journalctl --user -u codex-window-keeper.service -n 100 --no-pager
systemctl --user show-environment
```

The user manager may have a different `PATH` or may not inherit `CODEX_HOME`. Prefer absolute paths
in TOML and ensure pipx's `~/.local/bin` installation exists. After changing the unit:

```bash
systemctl --user daemon-reload
systemctl --user restart codex-window-keeper.service
```

For operation after logout, `loginctl enable-linger "$USER"` may be required by the host's policy;
the project never enables it automatically.

## Permission or state-file errors

The service refuses to execute if it cannot durably save the attempt first. Ensure the configured
state parent is writable. The default is `~/.local/state/codex-window-keeper/state.json`; files are
created mode 0600.

## Command timeout

Increase `command_timeout` only when the configured task legitimately needs longer. On timeout the
whole spawned process group receives SIGTERM, followed by SIGKILL. A timeout counts as a failed
attempt and uses the normal retry delay.
