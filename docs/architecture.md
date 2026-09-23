# Architecture

`codex-window-keeper` separates untrusted/private session input, pure scheduling decisions, durable
state, and process execution.

```text
detector -> WindowInfo -> decide_next (pure) -> Decision
                              |                    |
                         KeeperState          sleep/execute
                              ^                    |
                              +---- atomic state <-+
```

## Detector

The detector sorts session files by modification time, limits the candidate count, and reads a
bounded tail from each file. It parses individual JSONL records from newest to oldest. Recursive
dictionary traversal makes the `rate_limits` location tolerant of event wrappers, while strict
validation of the primary 300-minute window prevents the weekly window from being selected.

No raw record leaves the detector. Parse errors and unknown events are skipped.

## Durable state

The state file contains only reset timestamps, attempt count, and last-attempt time. An attempt is
persisted before starting a child. A successful exact reset timestamp is persisted before `--once`
returns. Writes use a mode-0600 temporary file in the destination directory, `fsync`, and atomic
`os.replace`.

## State machine

`decide_next` is a pure function with an injected timestamp. Its phases are:

```text
WAIT_FOR_STATUS -> WAIT_FOR_RESET -> GRACE_PERIOD -> EXECUTE
                                            failure -> RETRY -> EXECUTE
                                            success -> WAIT_NEW_RESET
```

Retry exhaustion is a stable state. A long-running service polls for a genuinely new reset rather
than repeatedly executing. `--once` returns nonzero after exhausting its initial attempt plus the
configured retry count.

The service never derives a future window from the prior reset. Only the detector can introduce a
new reset timestamp.

When `daily_at` is configured, the service selects a separate local-calendar state machine instead
of reset-based triggering. It waits for the configured `HH:MM`, uses `active_schedule_date` for
retry accounting, and records `last_scheduled_date` after success. This lets a restart catch up once
later the same day without duplicating a successful daily run. Session detection remains available
through `--status`, but it does not drive execution in daily mode.

## Process lifecycle

Built-in Codex modes use argv directly. Only explicit custom-command mode uses `/bin/sh -c`.
Children start in their own process group. Timeout and SIGINT/SIGTERM shutdown send SIGTERM to the
group and then SIGKILL if the grace period expires. Child stdout/stderr are inherited for journald.

Wall-clock waiting is behind a `Clock` protocol; tests use a fake clock and never wait for five
hours. Detector and executor interfaces are injected into service tests.
