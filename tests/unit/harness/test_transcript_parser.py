"""Unit tests for transcript parsing — pure parsing logic, no runtime state setup.

Extracted from tests/integration/ops/test_session_log.py per tier reclassification:
these tests create no spawn/session state and use no runtime root.
test_parse_session_file_splits_segments_on_compaction_boundary writes one input
file to tmp_path but exercises only the parsing contract.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.harness.transcript import DefaultTranscriptEventParser, parse_transcript_file


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
