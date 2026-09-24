"""Pure transcript row, boundary, and handoff parsing contracts."""

from __future__ import annotations

import pytest

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
    assistant_event = parser.parse(
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
    user_event = parser.parse(
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

    assistant, user = assistant_event.messages, user_event.messages
    assert assistant_event.boundary is user_event.boundary is False
    assert assistant_event.rendering_reason is user_event.rendering_reason is None
    assert _rows(assistant) == [
        ("assistant", "assistant text"),
        ("assistant", "[tool: Bash pwd]"),
    ]
    assert assistant[1].tool_call is not None
    assert (assistant[1].tool_call.name, assistant[1].tool_call.body) == ("bash", "pwd")
    assert _rows(user) == [("user", "user text"), ("user", "[tool_result] repo")]
    assert user[1].is_tool_result is True


@pytest.mark.parametrize("event_type", ["message", "message_end"])
def test_parser_extracts_pi_message_end_roles_and_tools(event_type: str) -> None:
    parser = DefaultTranscriptEventParser()

    def parse_message(message: dict[str, object]) -> list[TranscriptMessage]:
        parsed_event = parser.parse(
            {"event_type": event_type, "payload": {"type": event_type, "message": message}}
        )
        assert parsed_event.boundary is False
        assert parsed_event.rendering_reason is None
        return parsed_event.messages

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
    messages = [message for event in parsed for message in event.messages if not event.boundary]

    assert _rows(messages) == [
        ("assistant", "codex response"),
        ("assistant", '[tool: exec_command {"cmd":"pwd"}]'),
        ("user", "[tool_result] repo"),
        ("assistant", "codex exec"),
    ]
    assert messages[1].tool_call is not None
    assert (messages[1].tool_call.name, messages[1].tool_call.body) == ("bash", "pwd")
    assert messages[2].is_tool_result is True


def test_pi_journal_compaction_and_branch_annotations_survive_checkpoint() -> None:
    from meridian.lib.harness.transcript_preview import PreviewAccumulator

    events = [
        {"type": "session", "id": "session", "version": 3, "cwd": "/repo"},
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "message": {"role": "user", "content": "before"},
        },
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "message": {"role": "assistant", "content": "answer"},
        },
        {
            "type": "compaction",
            "id": "c",
            "parentId": "b",
            "summary": "recorded handoff",
            "firstKeptEntryId": "b",
            "tokensBefore": 10,
        },
        {
            "type": "custom_message",
            "id": "d",
            "parentId": "c",
            "customType": "notice",
            "content": "extension notice",
            "display": False,
        },
        {
            "type": "branch_summary",
            "id": "e",
            "parentId": "a",
            "fromId": "d",
            "summary": "branch summary",
        },
        {
            "type": "message",
            "id": "f",
            "parentId": "e",
            "message": {"role": "assistant", "content": "new branch answer"},
        },
    ]
    parsed = parse_transcript_events_with_prologues(events)
    assert parsed.total_compactions == 1
    assert parsed.segment_setups == (None, "recorded handoff")
    assert _rows(parsed.segments[0]) == [("user", "before"), ("assistant", "answer")]
    annotations = [m for m in parsed.segments[1] if m.kind == "annotation"]
    assert any("branch summary" in m.content for m in annotations)
    assert any("parent" in m.content.lower() for m in annotations)
    assert all(m.role == "annotation" for m in annotations)
    assert any(m.content == "extension notice" for m in parsed.segments[1])
    assert parsed.rendering_reason is None
    accumulator = PreviewAccumulator()
    for event in events:
        accumulator = PreviewAccumulator(accumulator.preview.model_copy())
        accumulator.feed(event)
    assert accumulator.preview.setup == "recorded handoff"
    assert accumulator.preview.pi_previous_entry_id == "f"
    assert any("parent" in line.lower() for line in accumulator.preview.lines())
    assert accumulator.preview.rendering_reason is None


def test_pi_unknown_material_is_not_certified_empty() -> None:
    from meridian.lib.harness.transcript_preview import PreviewAccumulator

    events = [
        {"type": "session", "id": "s", "version": 3, "cwd": "/repo"},
        {"type": "future_message", "id": "a", "parentId": None, "content": "not understood"},
    ]
    parsed = parse_transcript_events_with_prologues(events)
    assert parsed.rendering_reason
    accumulator = PreviewAccumulator()
    for event in events:
        accumulator.feed(event)
    assert accumulator.preview.rendering_reason
    assert "No messages" not in "\n".join(accumulator.preview.lines())


@pytest.mark.parametrize("event_type", [[], {}])
def test_pi_unhashable_type_is_incomplete_not_a_parser_crash(event_type: object) -> None:
    # Headerless native-shaped rows are not enough to claim Pi semantics, but
    # must remain safe for the generic shared parser.
    parsed = parse_transcript_events_with_prologues(
        [{"type": event_type, "id": "entry", "parentId": None}]
    )
    assert parsed.rendering_reason is None

    # Within a recognized Pi journal, malformed types retain fail-closed
    # rendering semantics rather than becoming a successful empty view.
    parsed = parse_transcript_events_with_prologues(
        [
            {"type": "session", "id": "session", "version": 3, "cwd": "/repo"},
            {"type": event_type, "id": "entry", "parentId": None},
        ]
    )
    assert parsed.rendering_reason == "Unsupported Pi journal entry; rendering is incomplete."


@pytest.mark.parametrize("event_type", [[], {}])
@pytest.mark.parametrize("missing_fields", [(), ("id",), ("parentId",), ("id", "parentId")])
def test_pi_malformed_type_without_entry_identity_is_incomplete(
    event_type: object, missing_fields: tuple[str, ...]
) -> None:
    from meridian.lib.harness.transcript_preview import PreviewAccumulator

    malformed = {"type": event_type, "id": "entry", "parentId": None}
    for field in missing_fields:
        malformed.pop(field)

    # Without an identifying Pi header, preserve the shared parser's generic
    # fallthrough even for a malformed native-looking row.
    generic = parse_transcript_events_with_prologues([malformed])
    assert generic.rendering_reason is None

    events = [{"type": "session", "id": "session", "version": 3, "cwd": "/repo"}, malformed]
    parsed = parse_transcript_events_with_prologues(events)
    assert parsed.rendering_reason == "Unsupported Pi journal entry; rendering is incomplete."

    accumulator = PreviewAccumulator()
    for event in events:
        accumulator.feed(event)
    assert accumulator.preview.rendering_reason == parsed.rendering_reason
    assert "No messages" not in "\n".join(accumulator.preview.lines())


def test_pi_bash_execution_and_metadata_journal() -> None:
    events = [
        {"type": "session", "version": 3, "id": "s", "cwd": "/repo"},
        {"type": "model_change", "id": "a", "parentId": None, "provider": "p", "modelId": "m"},
        {
            "type": "custom",
            "id": "b",
            "parentId": "a",
            "customType": "state",
            "data": {"text": "not a message"},
        },
        {
            "type": "message",
            "id": "c",
            "parentId": "b",
            "message": {
                "role": "bashExecution",
                "command": "pwd",
                "output": "/repo",
                "exitCode": 0,
            },
        },
    ]
    parsed = parse_transcript_events_with_prologues(events)
    assert parsed.rendering_reason is None
    assert parsed.total_compactions == 0
    assert _rows(parsed.segments[0]) == [
        ("user", "[tool: bash pwd]"),
        ("user", "[tool_result] /repo"),
    ]
    assert parsed.segments[0][0].tool_call is not None
    assert parsed.segments[0][1].is_tool_result


def test_non_pi_message_shape_does_not_start_pi_journal_tracking() -> None:
    parsed = parse_transcript_events_with_prologues(
        [
            {
                "type": "message",
                "id": "a",
                "parentId": None,
                "role": "assistant",
                "content": "generic",
            },
            {"type": "other_metadata"},
        ]
    )
    assert _rows(parsed.segments[0]) == [("assistant", "generic")]
    assert parsed.rendering_reason is None


def test_pi_preview_does_not_persist_unbounded_entry_identity() -> None:
    from meridian.lib.harness.transcript_preview import PreviewAccumulator

    accumulator = PreviewAccumulator()
    accumulator.feed({
        "type": "message", "id": "x" * 100_000, "parentId": None,
        "message": {"role": "assistant", "content": "answer"},
    })
    assert accumulator.preview.rendering_reason
    assert accumulator.preview.pi_previous_entry_id is None
    assert len(accumulator.preview.model_dump_json()) < 1000


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "content": [{"type": "text", "text": 42}]},
        {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": 42}]},
        {"role": "assistant", "content": [{"type": "toolCall", "name": None, "arguments": {}}]},
        {"role": "bashExecution", "command": 42, "output": "output"},
        {"role": "bashExecution", "command": "pwd", "output": None},
    ],
)
def test_malformed_pi_material_is_not_certified_empty(message: dict[str, object]) -> None:
    from meridian.lib.harness.transcript_preview import PreviewAccumulator

    event = {"type": "message", "message": message}
    parsed = parse_transcript_events_with_prologues([event])
    assert parsed.rendering_reason
    accumulator = PreviewAccumulator()
    accumulator.feed(event)
    assert accumulator.preview.rendering_reason
    assert "No messages" not in "\n".join(accumulator.preview.lines())
