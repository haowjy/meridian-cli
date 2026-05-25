"""Unit tests for deterministic session log entry navigation windows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.ops.session_log import SessionLogInput, session_log_sync


def _event(role: str, text: str) -> str:
    return json.dumps(
        {"type": role, "message": {"content": [{"type": "text", "text": text}]}}
    )


def _tool_use(name: str, command: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": name, "input": {"command": command}}
                ]
            },
        }
    )


def _tool_result(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": text}]},
        }
    )


def _write_session_file(path: Path) -> None:
    lines = [
        _event("user", "u1"),
        _event("assistant", "a1"),
        _event("user", "u2"),
        _tool_use("bash", "ls"),
        _tool_result("result"),
        _event("assistant", "a2"),
        _event("user", "u3"),
        _event("assistant", "a3"),
        _event("user", "u4"),
        _event("assistant", "a4"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_large_session_file(path: Path) -> str:
    large_text = "\n".join(f"line {index}" for index in range(1, 121))
    lines = [
        _event("user", "u1"),
        _event("assistant", large_text),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return large_text


def test_session_log_default_shows_recent_five_entries_in_current_segment(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    _write_session_file(session_file)

    output = session_log_sync(SessionLogInput(file_path=session_file.as_posix()))

    assert output.showing == "4-8"
    assert output.segment_index == 0
    assert output.segment_entries == 8
    assert [entry.index for entry in output.entries] == [4, 5, 6, 7, 8]
    assert output.hints == ("Use --full to show the entire selected segment.",)


def test_session_log_full_shows_entire_selected_segment(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    _write_session_file(session_file)

    output = session_log_sync(SessionLogInput(file_path=session_file.as_posix(), full=True))

    assert output.showing == "1-8"
    assert [entry.index for entry in output.entries] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert output.hints == ()


def test_session_log_entry_grouping_closes_on_tool_result(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    _write_session_file(session_file)

    output = session_log_sync(SessionLogInput(file_path=session_file.as_posix(), full=True))

    tool_entry = output.entries[2]
    assert tool_entry.index == 3
    assert tool_entry.role == "mixed"
    assert [message.role for message in tool_entry.messages] == ["user", "assistant", "user"]
    assert tool_entry.segment_start_message == 3
    assert tool_entry.segment_end_message == 5


def test_session_log_header_preserves_requested_ref_when_resolved_id_differs() -> None:
    from meridian.lib.ops.session_log import (
        SessionLogEntry,
        SessionLogEntryMessage,
        SessionLogOutput,
    )

    output = SessionLogOutput(
        session_id="harness-session-123",
        requested_ref="p2490",
        source="codex transcript",
        total_entries=1,
        total_segments=1,
        showing="1-1",
        entries=(
            SessionLogEntry(
                index=1,
                segment=0,
                segment_start_message=1,
                segment_end_message=1,
                role="assistant",
                content="done",
                messages=(
                    SessionLogEntryMessage(
                        segment_message=1,
                        role="assistant",
                        content="done",
                    ),
                ),
            ),
        ),
    )

    assert output.format_text().splitlines()[0] == (
        "Session p2490 (codex transcript: harness-session-123) — "
        "showing 1-1 of 1 entry"
    )


def test_session_log_tail_and_absolute_window_are_deterministic(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    _write_session_file(session_file)

    tail_output = session_log_sync(SessionLogInput(file_path=session_file.as_posix(), tail=1))
    assert [entry.index for entry in tail_output.entries] == [8]

    around_output = session_log_sync(
        SessionLogInput(file_path=session_file.as_posix(), around_ordinal=3, context=1)
    )
    assert [entry.index for entry in around_output.entries] == [2, 3, 4]
    assert around_output.next_command is not None
    assert "--from 5 --limit 3" in around_output.next_command


def test_session_log_rejects_conflicting_selectors(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    _write_session_file(session_file)

    with pytest.raises(ValueError, match="cannot be combined"):
        session_log_sync(
            SessionLogInput(
                file_path=session_file.as_posix(),
                from_ordinal=2,
                limit=1,
                tail=1,
            )
        )

    with pytest.raises(ValueError, match="cannot be combined"):
        session_log_sync(
            SessionLogInput(
                file_path=session_file.as_posix(),
                full=True,
                tail=1,
            )
        )


def test_session_log_truncates_oversized_content_by_default(tmp_path: Path) -> None:
    session_file = tmp_path / "session-large.jsonl"
    _write_large_session_file(session_file)

    output = session_log_sync(SessionLogInput(file_path=session_file.as_posix(), full=True))

    assistant_content = output.entries[1].messages[0].content
    assert "truncated: omitted 40 lines" in assistant_content
    assert "rerun with --no-truncate" in assistant_content
    assert output.entries[1].content == assistant_content


def test_session_log_no_truncate_preserves_full_content(tmp_path: Path) -> None:
    session_file = tmp_path / "session-large.jsonl"
    large_text = _write_large_session_file(session_file)

    output = session_log_sync(
        SessionLogInput(file_path=session_file.as_posix(), full=True, truncate=False)
    )

    assert output.entries[1].messages[0].content == large_text
    assert output.entries[1].content == large_text
