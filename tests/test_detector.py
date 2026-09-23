from __future__ import annotations

import os
from pathlib import Path

import pytest

from codex_window_keeper.detector import (
    detect_latest_window,
    detect_window_in_file,
    discover_session_files,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _session_path(codex_home: Path, name: str) -> Path:
    path = codex_home / "sessions" / "2026" / "09" / "21" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_detects_normal_nested_jsonl() -> None:
    result = detect_window_in_file(FIXTURES / "codex_session_normal.jsonl", 2 * 1024 * 1024)

    assert result is not None
    assert result.window_minutes == 300
    assert result.used_percent == 42.5
    assert result.resets_at == 1_800_000_000


def test_selects_primary_300_minute_window_not_weekly_window() -> None:
    result = detect_window_in_file(
        FIXTURES / "codex_session_multiple_windows.jsonl", 2 * 1024 * 1024
    )

    assert result is not None
    assert result.window_minutes == 300
    assert result.resets_at == 1_800_000_002


def test_skips_corrupt_and_incomplete_json_lines() -> None:
    result = detect_window_in_file(FIXTURES / "codex_session_corrupt.jsonl", 2 * 1024 * 1024)

    assert result is not None
    assert result.resets_at == 1_800_000_001


def test_searches_older_file_when_newest_has_no_rate_limits(tmp_path: Path) -> None:
    older = _session_path(tmp_path, "older.jsonl")
    older.write_text(
        '{"rate_limits":{"primary":{"window_minutes":300,"resets_at":1800000003}}}\n',
        encoding="utf-8",
    )
    newest = _session_path(tmp_path, "newest.jsonl")
    newest.write_text('{"type":"unrelated"}\n', encoding="utf-8")
    os.utime(older, (100, 100))
    os.utime(newest, (200, 200))

    result = detect_latest_window(tmp_path, max_files=2, tail_bytes=4096)

    assert result is not None
    assert result.source_file == older
    assert result.resets_at == 1_800_000_003


def test_uses_latest_window_event_within_file(tmp_path: Path) -> None:
    session = _session_path(tmp_path, "multiple.jsonl")
    session.write_text(
        "\n".join(
            [
                '{"rate_limits":{"primary":{"window_minutes":300,"resets_at":1800000004}}}',
                '{"rate_limits":{"primary":{"window_minutes":300,"resets_at":1800000005}}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = detect_window_in_file(session, tail_bytes=4096)

    assert result is not None
    assert result.resets_at == 1_800_000_005


def test_large_file_scan_is_limited_to_tail(tmp_path: Path) -> None:
    session = _session_path(tmp_path, "large.jsonl")
    old_event = (
        '{"rate_limits":{"primary":{"window_minutes":300,"resets_at":1800000006}}}\n'
    )
    padding = '{"type":"padding","value":"' + ("x" * 200) + '"}\n'
    session.write_text(old_event + (padding * 500), encoding="utf-8")

    assert session.stat().st_size > 1024
    assert detect_window_in_file(session, tail_bytes=1024) is None

    with session.open("a", encoding="utf-8") as output:
        output.write(
            '{"rate_limits":{"primary":{"window_minutes":300,'
            '"resets_at":1800000007}}}\n'
        )

    result = detect_window_in_file(session, tail_bytes=1024)
    assert result is not None
    assert result.resets_at == 1_800_000_007


def test_direct_rate_limits_shape_is_supported(tmp_path: Path) -> None:
    session = _session_path(tmp_path, "direct.jsonl")
    session.write_text(
        '{"rate_limits":{"primary":{"used_percent":7,"window_minutes":300,'
        '"resets_at":1800000008}}}\n',
        encoding="utf-8",
    )

    result = detect_window_in_file(session, 4096)

    assert result is not None
    assert result.used_percent == 7.0


@pytest.mark.parametrize(
    "primary",
    [
        '{"window_minutes":60,"resets_at":1800000009}',
        '{"window_minutes":300}',
        '{"window_minutes":300,"resets_at":-1}',
        '{"window_minutes":300,"resets_at":"1800000009"}',
        '{"window_minutes":true,"resets_at":1800000009}',
    ],
)
def test_rejects_invalid_primary_metadata(tmp_path: Path, primary: str) -> None:
    session = _session_path(tmp_path, "invalid.jsonl")
    session.write_text(f'{{"rate_limits":{{"primary":{primary}}}}}\n', encoding="utf-8")

    assert detect_window_in_file(session, 4096) is None


def test_file_limit_and_mtime_order_are_enforced(tmp_path: Path) -> None:
    older = _session_path(tmp_path, "older-valid.jsonl")
    older.write_text(
        '{"rate_limits":{"primary":{"window_minutes":300,"resets_at":1800000010}}}\n',
        encoding="utf-8",
    )
    newest = _session_path(tmp_path, "newest-empty.jsonl")
    newest.write_text("{}\n", encoding="utf-8")
    os.utime(older, (100, 100))
    os.utime(newest, (200, 200))

    assert discover_session_files(tmp_path, 1) == [newest]
    assert detect_latest_window(tmp_path, max_files=1, tail_bytes=4096) is None


def test_missing_sessions_directory_returns_none(tmp_path: Path) -> None:
    assert detect_latest_window(tmp_path, max_files=50, tail_bytes=4096) is None


@pytest.mark.parametrize(("max_files", "tail_bytes"), [(0, 1), (1, 0)])
def test_scan_limits_must_be_positive(tmp_path: Path, max_files: int, tail_bytes: int) -> None:
    if max_files == 0:
        with pytest.raises(ValueError, match="max_files"):
            detect_latest_window(tmp_path, max_files=max_files, tail_bytes=tail_bytes)
    else:
        session = _session_path(tmp_path, "anything.jsonl")
        session.write_text("{}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="tail_bytes"):
            detect_latest_window(tmp_path, max_files=max_files, tail_bytes=tail_bytes)
