from typing import Any

from meridian.lib.chat.normalization.claude import ClaudeNormalizer
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.streaming.drain_policy import TURN_BOUNDARY_EVENT_TYPE


def event(event_type: str, payload: dict[str, Any]) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, payload=payload, harness_id="claude")


def test_claude_text_sequence_normalizes_turn_and_content():
    n = ClaudeNormalizer("c1", "s1")

    started = n.normalize(
        event("message_start", {"type": "message_start", "message": {"model": "claude-opus"}})
    )[0]
    delta = n.normalize(
        event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hi"}})
    )[0]
    completed = n.normalize(
        event(
            "result", {"status": "succeeded", "usage": {"input_tokens": 1}, "total_cost_usd": 0.01}
        )
    )[0]

    assert started.type == "turn.started"
    assert started.payload["model"] == "claude-opus"
    assert delta.type == "content.delta"
    assert delta.payload == {"stream_kind": "assistant_text", "text": "hi"}
    assert delta.turn_id == started.turn_id
    assert completed.type == "turn.completed"
    assert completed.payload["cost_usd"] == 0.01


def test_claude_result_error_variant_maps_terminal_fields_and_deduplicates_completion():
    n = ClaudeNormalizer("c1", "s1")
    started = n.normalize(event("message_start", {"message": {"model": "claude-sonnet"}}))[0]

    completed = n.normalize(
        event(
            "result",
            {
                "status": "failed",
                "error": "tool rejected",
                "exit_code": 130,
                "terminal_reason": "interrupted",
                "usage": {"output_tokens": 2},
                "duration_ms": 42,
                "total_cost_usd": 0.25,
            },
        )
    )[0]

    assert completed.type == "turn.completed"
    assert completed.turn_id == started.turn_id
    assert completed.payload == {
        "status": "failed",
        "error": "tool rejected",
        "exit_code": 130,
        "usage": {"output_tokens": 2},
        "duration_ms": 42,
        "cost_usd": 0.25,
    }
    assert "terminal_reason" not in completed.payload
    assert n.normalize(
        HarnessEvent(
            event_type=TURN_BOUNDARY_EVENT_TYPE,
            payload={"status": "failed", "synthetic": True},
            harness_id="meridian",
        )
    ) == []


def test_claude_tool_json_accumulates_across_deltas_and_completes_with_full_input():
    n = ClaudeNormalizer("c1", "s1")
    n.normalize(event("message_start", {"message": {}}))

    started = n.normalize(
        event(
            "content_block_start",
            {"index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read"}},
        )
    )[0]
    first = n.normalize(
        event(
            "content_block_delta",
            {"index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"file"'}},
        )
    )[0]
    second = n.normalize(
        event(
            "content_block_delta",
            {"index": 2, "delta": {"type": "input_json_delta", "partial_json": ':"a.txt"}'}},
        )
    )[0]
    completed = n.normalize(event("content_block_stop", {"index": 2}))[0]

    assert started.type == "item.started"
    assert started.item_id == "toolu_1"
    assert first.type == "item.updated"
    assert first.payload == {"input_json_delta": '{"file"', "input_json": '{"file"'}
    assert second.payload == {
        "input_json_delta": ':"a.txt"}',
        "input_json": '{"file":"a.txt"}',
    }
    assert completed.type == "item.completed"
    assert completed.payload == {
        "item_type": "Read",
        "name": "Read",
        "input_json": '{"file":"a.txt"}',
    }


def test_claude_reasoning_and_unknown_deltas_follow_current_behavior():
    n = ClaudeNormalizer("c1", "s1")
    n.normalize(event("message_start", {"message": {}}))

    reasoning = n.normalize(
        event(
            "content_block_delta",
            {"index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        )
    )[0]

    assert reasoning.payload == {"stream_kind": "reasoning_text", "text": "hmm"}
    assert n.normalize(
        event(
            "content_block_delta",
            {"index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
        )
    ) == []
    assert n.normalize(event("surprise", {})) == []


def test_claude_result_emits_files_before_turn_completed():
    n = ClaudeNormalizer("c1", "s1")
    n.normalize(event("message_start", {"message": {}}))

    files, completed = n.normalize(
        event(
            "result",
            {
                "status": "succeeded",
                "files": ["notes.txt", {"path": "report.txt", "operation": "write"}],
            },
        )
    )

    assert files.type == "files.persisted"
    assert files.payload == {
        "files": [
            {"path": "notes.txt"},
            {"path": "report.txt", "operation": "write"},
        ]
    }
    assert completed.type == "turn.completed"


def test_claude_aggregated_assistant_event_emits_reasoning_and_text_content():
    n = ClaudeNormalizer("c1", "s1")

    started, reasoning, text = n.normalize(
        event(
            "assistant",
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "checking"},
                        {"type": "text", "text": "Hello! How can I help?"},
                    ]
                },
            },
        )
    )
    completed = n.normalize(
        event("result", {"status": "succeeded", "result": "Hello! How can I help?"})
    )[0]

    assert started.type == "turn.started"
    assert reasoning.type == "content.delta"
    assert reasoning.payload == {"stream_kind": "reasoning_text", "text": "checking"}
    assert text.type == "content.delta"
    assert text.payload == {"stream_kind": "assistant_text", "text": "Hello! How can I help?"}
    assert text.turn_id == started.turn_id
    assert completed.type == "turn.completed"
    assert completed.turn_id == started.turn_id


def test_claude_result_text_emits_assistant_content_when_no_assistant_event_present():
    n = ClaudeNormalizer("c1", "s1")

    started, text, completed = n.normalize(
        event("result", {"status": "succeeded", "result": "result-only reply"})
    )

    assert started.type == "turn.started"
    assert text.type == "content.delta"
    assert text.payload == {"stream_kind": "assistant_text", "text": "result-only reply"}
    assert text.turn_id == started.turn_id
    assert completed.type == "turn.completed"
    assert completed.turn_id == started.turn_id
