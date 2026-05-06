from typing import Any

from meridian.lib.chat.normalization.claude import ClaudeNormalizer
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.streaming.drain_policy import TURN_BOUNDARY_EVENT_TYPE


def event(event_type: str, payload: dict[str, Any], harness_id: str = "claude") -> HarnessEvent:
    return HarnessEvent(event_type=event_type, payload=payload, harness_id=harness_id)


def test_claude_aggregated_tool_use_and_tool_result_map_to_item_lifecycle():
    n = ClaudeNormalizer("c1", "s1")

    started, tool_started = n.normalize(
        event(
            "assistant",
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Read",
                            "input": {"file": "a.txt"},
                        }
                    ]
                }
            },
        )
    )
    tool_completed = n.normalize(
        event(
            "user",
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "ok",
                            "is_error": True,
                        }
                    ]
                }
            },
        )
    )[0]

    assert started.type == "turn.started"
    assert tool_started.type == "item.started"
    assert tool_started.item_id == "toolu_1"
    assert tool_started.payload == {
        "item_type": "Read",
        "name": "Read",
        "raw_type": "tool_use",
        "input": {"file": "a.txt"},
    }
    assert tool_completed.type == "item.completed"
    assert tool_completed.item_id == "toolu_1"
    assert tool_completed.payload == {
        "item_type": "Read",
        "name": "Read",
        "raw_type": "tool_result",
        "content": "ok",
        "is_error": True,
        "error": "ok",
        "input": {"file": "a.txt"},
    }


def test_claude_synthetic_completion_after_real_completion_does_not_break_next_turn():
    n = ClaudeNormalizer("c1", "s1")

    first_started, first_text = n.normalize(
        event("assistant", {"message": {"content": [{"type": "text", "text": "first"}]}})
    )
    first_completed = n.normalize(event("result", {"status": "succeeded"}))[0]
    synthetic = n.normalize(
        event(
            TURN_BOUNDARY_EVENT_TYPE,
            {"status": "succeeded", "synthetic": True},
            harness_id="meridian",
        )
    )
    second_started, second_text = n.normalize(
        event("assistant", {"message": {"content": [{"type": "text", "text": "second"}]}})
    )
    second_completed = n.normalize(event("result", {"status": "succeeded"}))[0]

    assert first_started.type == "turn.started"
    assert first_text.type == "content.delta"
    assert first_completed.type == "turn.completed"
    assert synthetic == []
    assert second_started.type == "turn.started"
    assert second_started.turn_id != first_started.turn_id
    assert second_text.payload == {"stream_kind": "assistant_text", "text": "second"}
    assert second_completed.type == "turn.completed"
    assert second_completed.turn_id == second_started.turn_id


def test_claude_later_turn_aggregated_tool_result_survives_prior_synthetic_completion():
    n = ClaudeNormalizer("c1", "s1")

    first_started, first_text = n.normalize(
        event("assistant", {"message": {"content": [{"type": "text", "text": "first"}]}})
    )
    first_completed = n.normalize(event("result", {"status": "succeeded"}))[0]
    synthetic = n.normalize(
        event(
            TURN_BOUNDARY_EVENT_TYPE,
            {"status": "succeeded", "synthetic": True},
            harness_id="meridian",
        )
    )

    second_started, tool_started = n.normalize(
        event(
            "assistant",
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_2",
                            "name": "Read",
                            "input": {"file": "b.txt"},
                        }
                    ]
                }
            },
        )
    )
    tool_completed = n.normalize(
        event(
            "user",
            {
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}]
                }
            },
        )
    )[0]
    second_completed = n.normalize(event("result", {"status": "succeeded"}))[0]

    assert first_started.type == "turn.started"
    assert first_text.type == "content.delta"
    assert first_completed.type == "turn.completed"
    assert synthetic == []
    assert second_started.type == "turn.started"
    assert second_started.turn_id != first_started.turn_id
    assert tool_started.type == "item.started"
    assert tool_started.turn_id == second_started.turn_id
    assert tool_started.item_id == "toolu_2"
    assert tool_completed.type == "item.completed"
    assert tool_completed.turn_id == second_started.turn_id
    assert tool_completed.item_id == "toolu_2"
    assert tool_completed.payload["input"] == {"file": "b.txt"}
    assert second_completed.type == "turn.completed"
    assert second_completed.turn_id == second_started.turn_id


def test_claude_streaming_blocks_still_work():
    n = ClaudeNormalizer("c1", "s1")
    started = n.normalize(event("message_start", {"message": {"model": "claude-opus"}}))[0]
    delta = n.normalize(
        event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hi"}})
    )[0]
    completed = n.normalize(
        event(
            "result",
            {"status": "succeeded", "usage": {"input_tokens": 1}, "total_cost_usd": 0.01},
        )
    )[0]

    assert started.type == "turn.started"
    assert started.payload["model"] == "claude-opus"
    assert delta.type == "content.delta"
    assert delta.payload == {"stream_kind": "assistant_text", "text": "hi"}
    assert completed.type == "turn.completed"
    assert completed.payload["cost_usd"] == 0.01


def test_claude_tool_use_block_stop_is_non_terminal_until_tool_result():
    n = ClaudeNormalizer("c1", "s1")
    n.normalize(event("message_start", {"message": {"model": "claude-opus"}}))

    started = n.normalize(
        event(
            "content_block_start",
            {
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read"},
            },
        )
    )[0]
    n.normalize(
        event(
            "content_block_delta",
            {
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"file":"a.txt"}'},
            },
        )
    )
    stop = n.normalize(event("content_block_stop", {"index": 0}))[0]
    completed = n.normalize(
        event(
            "user",
            {
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]
                }
            },
        )
    )[0]

    assert started.type == "item.started"
    assert stop.type == "item.updated"
    assert stop.item_id == "toolu_1"
    assert stop.payload["input_json"] == '{"file":"a.txt"}'
    assert completed.type == "item.completed"
    assert completed.item_id == "toolu_1"
    assert completed.payload["input"] == '{"file":"a.txt"}'


def test_claude_result_error_variant_maps_terminal_fields():
    n = ClaudeNormalizer("c1", "s1")
    n.normalize(event("message_start", {"message": {"model": "claude-sonnet"}}))

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
    assert completed.payload == {
        "status": "failed",
        "error": "tool rejected",
        "exit_code": 130,
        "usage": {"output_tokens": 2},
        "duration_ms": 42,
        "cost_usd": 0.25,
    }


def test_claude_result_text_emits_assistant_content_when_no_assistant_event_present():
    n = ClaudeNormalizer("c1", "s1")

    started, text, completed = n.normalize(
        event("result", {"status": "succeeded", "result": "result-only reply"})
    )

    assert started.type == "turn.started"
    assert text.type == "content.delta"
    assert text.payload == {"stream_kind": "assistant_text", "text": "result-only reply"}
    assert completed.type == "turn.completed"
