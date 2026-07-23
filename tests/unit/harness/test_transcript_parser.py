"""Pure transcript row, boundary, and handoff parsing contracts."""

from __future__ import annotations

from meridian.lib.harness.transcript import (
    DefaultTranscriptEventParser,
    TranscriptMessage,
    parse_transcript_events,
    parse_transcript_events_with_prologues,
)


def _rows(segment: list[TranscriptMessage]) -> list[tuple[str, str]]:
    return [(message.role, message.content) for message in segment]


def test_events_split_claude_boundary_and_preserve_prologues() -> None:
    events = [
        {"type": "system", "content": "initial prompt"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "before boundary"}]},
        },
        {"type": "system", "subtype": "compact_boundary", "summary": "handoff"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "after boundary"}]},
        },
    ]

    parsed = parse_transcript_events_with_prologues(events)
    segments, total_compactions = parse_transcript_events(events)

    assert total_compactions == parsed.total_compactions == 1
    assert [_rows(segment) for segment in segments] == [
        [("assistant", "before boundary")],
        [("assistant", "after boundary")],
    ]
    assert parsed.segment_prologues == ("initial prompt", "handoff")


def test_boundary_without_summary_allocates_empty_next_setup_slot() -> None:
    parsed = parse_transcript_events_with_prologues(
        [
            {"role": "assistant", "content": "before"},
            {"type": "system", "subtype": "compact_boundary"},
            {"role": "assistant", "content": "after"},
        ]
    )

    assert parsed.total_compactions == 1
    assert parsed.segment_setups == (None, None)
    assert [_rows(segment) for segment in parsed.segments] == [
        [("assistant", "before")],
        [("assistant", "after")],
    ]


def test_claude_boundary_consumes_synthetic_follow_on_handoff() -> None:
    parsed = parse_transcript_events_with_prologues(
        [
            {"type": "system", "content": "initial prompt"},
            {"role": "assistant", "content": "segment0"},
            {"type": "system", "subtype": "compact_boundary"},
            {
                "type": "user",
                "isSynthetic": True,
                "message": {"content": [{"type": "text", "text": "synthetic handoff"}]},
            },
            {"role": "assistant", "content": "segment1"},
        ]
    )

    assert parsed.segment_setups == ("initial prompt", "synthetic handoff")
    assert parsed.consumed_setup_event_indexes == (3,)
    assert _rows(parsed.segments[1]) == [("assistant", "segment1")]


def test_opencode_boundary_consumes_compaction_agent_handoff() -> None:
    parsed = parse_transcript_events_with_prologues(
        [
            {"role": "assistant", "content": "segment0"},
            {"part": {"type": "compaction"}},
            {
                "role": "assistant",
                "mode": "compaction",
                "agent": "compaction",
                "parts": [{"type": "text", "text": "opencode handoff"}],
                "content": "opencode handoff",
            },
            {"role": "assistant", "content": "segment1"},
        ]
    )

    assert parsed.total_compactions == 1
    assert parsed.segment_setups == (None, "opencode handoff")
    assert parsed.consumed_setup_event_indexes == (2,)
    assert _rows(parsed.segments[1]) == [("assistant", "segment1")]


def test_parser_extracts_claude_messages_tool_call_and_result() -> None:
    parser = DefaultTranscriptEventParser()
    assistant, assistant_boundary = parser.parse(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "assistant text"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                ]
            },
        }
    )
    user, user_boundary = parser.parse(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "user text"},
                    {"type": "tool_result", "content": "repo"},
                ]
            },
        }
    )

    assert assistant_boundary is user_boundary is False
    assert _rows(assistant) == [
        ("assistant", "assistant text"),
        ("assistant", "[tool: Bash pwd]"),
    ]
    assert assistant[1].tool_call is not None
    assert (assistant[1].tool_call.name, assistant[1].tool_call.body) == ("bash", "pwd")
    assert _rows(user) == [("user", "user text"), ("user", "[tool_result] repo")]
    assert user[1].is_tool_result is True


def test_parser_extracts_pi_message_end_roles_and_tools() -> None:
    parser = DefaultTranscriptEventParser()

    def parse_message(message: dict[str, object]) -> list[TranscriptMessage]:
        rows, boundary = parser.parse(
            {"event_type": "message_end", "payload": {"type": "message_end", "message": message}}
        )
        assert boundary is False
        return rows

    user = parse_message({"role": "user", "content": [{"type": "text", "text": "task"}]})
    call = parse_message(
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "toolCall", "name": "bash_manage", "arguments": {"action": "kill"}},
            ],
        }
    )
    custom = parse_message({"role": "custom", "content": "Background task running"})
    result = parse_message({"role": "toolResult", "content": [{"type": "text", "text": "killed"}]})

    assert _rows(user) == [("user", "task")]
    assert _rows(call) == [("assistant", '[tool: bash_manage {"action":"kill"}]')]
    assert call[0].tool_call is not None
    assert _rows(custom) == [("user", "Background task running")]
    assert _rows(result) == [("user", "[tool_result] killed")]
    assert result[0].is_tool_result is True


def test_parser_extracts_codex_messages_tool_calls_and_results() -> None:
    parser = DefaultTranscriptEventParser()
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "codex response"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"pwd"}',
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "repo"},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "codex exec"}},
    ]

    parsed = [parser.parse(event) for event in events]
    messages = [message for rows, boundary in parsed for message in rows if boundary is False]

    assert _rows(messages) == [
        ("assistant", "codex response"),
        ("assistant", '[tool: exec_command {"cmd":"pwd"}]'),
        ("user", "[tool_result] repo"),
        ("assistant", "codex exec"),
    ]
    assert messages[1].tool_call is not None
    assert (messages[1].tool_call.name, messages[1].tool_call.body) == ("bash", "pwd")
    assert messages[2].is_tool_result is True
