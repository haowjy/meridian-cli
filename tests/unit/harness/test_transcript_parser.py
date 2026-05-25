"""Unit tests for transcript parsing — pure parsing logic, no runtime state setup.

Extracted from tests/integration/ops/test_session_log.py per tier reclassification:
these tests create no spawn/session state and use no runtime root.
test_parse_session_file_splits_segments_on_compaction_boundary writes one input
file to tmp_path but exercises only the parsing contract.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from meridian.lib.harness.transcript import (
    DefaultTranscriptEventParser,
    parse_transcript_file,
    parse_transcript_file_with_prologues,
)


def test_parse_session_file_splits_segments_on_compaction_boundary(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "before boundary"}]},
            }
        ),
        json.dumps({"type": "system", "subtype": "compact_boundary"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "after boundary"}]},
            }
        ),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    segments, total_compactions = parse_transcript_file(session_file)

    assert total_compactions == 1
    assert len(segments) == 2
    assert [(message.role, message.content) for message in segments[0]] == [
        ("assistant", "before boundary")
    ]
    assert [(message.role, message.content) for message in segments[1]] == [
        ("assistant", "after boundary")
    ]


def test_parse_session_file_extracts_segment_prologues(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    lines = [
        json.dumps({"type": "system", "content": "initial prompt"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "before boundary"}]},
            }
        ),
        json.dumps({"type": "system", "subtype": "compact_boundary", "summary": "handoff"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "after boundary"}]},
            }
        ),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed = parse_transcript_file_with_prologues(session_file)

    assert parsed.total_compactions == 1
    assert parsed.segment_prologues == ("initial prompt", "handoff")


def test_parse_session_file_boundary_without_summary_still_allocates_next_setup_slot(
    tmp_path: Path,
) -> None:
    session_file = tmp_path / "session.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "before boundary"}]},
            }
        ),
        json.dumps({"type": "system", "subtype": "compact_boundary"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "after boundary"}]},
            }
        ),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed = parse_transcript_file_with_prologues(session_file)

    assert parsed.total_compactions == 1
    assert len(parsed.segments) == 2
    assert parsed.segment_setups == (None, None)


def test_parse_session_file_consumes_synthetic_follow_on_handoff(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    lines = [
        json.dumps({"type": "system", "content": "initial prompt"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "before boundary"}]},
            }
        ),
        json.dumps({"type": "system", "subtype": "compact_boundary"}),
        json.dumps(
            {
                "type": "user",
                "isSynthetic": True,
                "message": {"content": [{"type": "text", "text": "synthetic handoff"}]},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "after boundary"}]},
            }
        ),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed = parse_transcript_file_with_prologues(session_file)

    assert parsed.segment_setups == ("initial prompt", "synthetic handoff")
    assert parsed.consumed_setup_event_indexes == (3,)
    assert [(message.role, message.content) for message in parsed.segments[1]] == [
        ("assistant", "after boundary")
    ]


def test_parse_session_file_consumes_opencode_follow_on_handoff(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    lines = [
        json.dumps({"role": "assistant", "content": "segment0"}),
        json.dumps({"part": {"type": "compaction"}}),
        json.dumps(
            {
                "role": "assistant",
                "mode": "compaction",
                "agent": "compaction",
                "parts": [{"type": "text", "text": "opencode handoff"}],
                "content": "opencode handoff",
            }
        ),
        json.dumps({"role": "assistant", "content": "segment1"}),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed = parse_transcript_file_with_prologues(session_file)

    assert parsed.total_compactions == 1
    assert parsed.segment_setups == (None, "opencode handoff")
    assert parsed.consumed_setup_event_indexes == (2,)
    assert [(message.role, message.content) for message in parsed.segments[1]] == [
        ("assistant", "segment1")
    ]


def test_parse_session_file_supports_json_array_lines(tmp_path: Path) -> None:
    session_file = tmp_path / "session.json"
    session_file.write_text(
        json.dumps(
            [
                {"role": "user", "content": "array user"},
                {"role": "assistant", "content": "array assistant"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    segments, total_compactions = parse_transcript_file(session_file)

    assert total_compactions == 0
    assert len(segments) == 1
    assert [(message.role, message.content) for message in segments[0]] == [
        ("user", "array user"),
        ("assistant", "array assistant"),
    ]


def test_parse_session_file_prefers_opencode_db_transcript(tmp_path: Path) -> None:
    session_id = "ses_fixture_db_1"
    session_file = tmp_path / "opencode" / "storage" / "session_diff" / f"{session_id}.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("[]\n", encoding="utf-8")

    db_path = tmp_path / "opencode" / "opencode.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO message "
            "(id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            ("msg_1", session_id, 1, 1, json.dumps({"role": "assistant"})),
        )
        connection.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("prt_1", "msg_1", session_id, 1, 1, json.dumps({"type": "text", "text": "db text"})),
        )
        connection.commit()

    segments, total_compactions = parse_transcript_file(session_file)

    assert total_compactions == 0
    assert len(segments) == 1
    assert [(message.role, message.content) for message in segments[0]] == [
        ("assistant", "db text")
    ]


def test_extract_from_event_claude_assistant_and_user_messages() -> None:
    assistant_messages, assistant_boundary = DefaultTranscriptEventParser().parse(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "assistant text"}]},
        }
    )
    user_messages, user_boundary = DefaultTranscriptEventParser().parse(
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "user text"}]},
        }
    )

    assert assistant_boundary is False
    assert user_boundary is False
    assert [(message.role, message.content) for message in assistant_messages] == [
        ("assistant", "assistant text")
    ]
    assert [(message.role, message.content) for message in user_messages] == [("user", "user text")]


def test_extract_from_event_codex_response_and_exec_events() -> None:
    response_messages, response_boundary = DefaultTranscriptEventParser().parse(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "codex response"}],
            },
        }
    )
    exec_messages, exec_boundary = DefaultTranscriptEventParser().parse(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "codex exec"},
        }
    )

    assert response_boundary is False
    assert exec_boundary is False
    assert [(message.role, message.content) for message in response_messages] == [
        ("assistant", "codex response")
    ]
    assert [(message.role, message.content) for message in exec_messages] == [
        ("assistant", "codex exec")
    ]
