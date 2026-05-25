"""Unit tests for session-log command rendering across platforms."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from meridian.lib.core import command_strings
from meridian.lib.ops.session_target import SessionLogTarget
from meridian.lib.ops.session_transcript import (
    SessionLogRoute,
    build_session_log_command,
    route_for_corpus_target,
)


def test_build_session_log_command_quotes_posix_file_paths(monkeypatch) -> None:
    monkeypatch.setattr(command_strings, "IS_WINDOWS", False)
    route = SessionLogRoute(mode="file", value="/tmp/segment logs/$latest&(1).jsonl")

    command = build_session_log_command(
        route,
        segment_index=2,
        around_ordinal=4,
        context=5,
    )

    assert (
        command
        == "meridian session log --file '/tmp/segment logs/$latest&(1).jsonl' "
        "--segment 2 --around 4 --context 5"
    )


def test_build_session_log_command_quotes_posix_refs(monkeypatch) -> None:
    monkeypatch.setattr(command_strings, "IS_WINDOWS", False)
    route = SessionLogRoute(mode="ref", value="chat ref;rm -rf /")

    command = build_session_log_command(route, before_ordinal=8, limit=3)

    assert command == "meridian session log 'chat ref;rm -rf /' --before 8 --limit 3"


def test_build_session_log_command_uses_windows_cmdline_rendering(monkeypatch) -> None:
    monkeypatch.setattr(command_strings, "IS_WINDOWS", True)
    route = SessionLogRoute(mode="file", value=r"C:\Users\Jane Doe\logs\session&(1).jsonl")

    command = build_session_log_command(route, segment_index=1, from_ordinal=0, limit=1)

    assert (
        command
        == 'meridian session log --file "C:\\Users\\Jane Doe\\logs\\session&(1).jsonl" '
        "--segment 1 --from 0 --limit 1"
    )


def test_route_for_corpus_target_uses_native_path_string() -> None:
    class _WindowsLikePath:
        def __str__(self) -> str:
            return r"C:\Users\Jane Doe\logs\session&(1).jsonl"

        def as_posix(self) -> str:
            return "C:/Users/Jane Doe/logs/session&(1).jsonl"

    target = SessionLogTarget(
        session_id="session-1",
        harness="codex",
        file_path=cast("Path", _WindowsLikePath()),
        source="codex transcript",
    )

    route = route_for_corpus_target(target)

    assert route.mode == "file"
    assert route.value == r"C:\Users\Jane Doe\logs\session&(1).jsonl"
