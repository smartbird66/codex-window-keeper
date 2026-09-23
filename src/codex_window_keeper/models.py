"""Shared data models."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """A detected Codex five-hour rate-limit window."""

    window_minutes: int
    used_percent: float | None
    resets_at: int
    source_file: Path


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Result of one child-process invocation."""

    returncode: int
    started_at: float
    finished_at: float
    timed_out: bool
    interrupted: bool = False
    error: str | None = None
