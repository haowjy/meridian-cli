from meridian.lib.chat.normalization.opencode import OpenCodeNormalizer
from meridian.lib.harness.connections.base import HarnessEvent


def event(event_type, payload):
    return HarnessEvent(event_type=event_type, payload=payload, harness_id="opencode")


def test_opencode_stream_starts_turn_and_completes_on_idle():
    n = OpenCodeNormalizer("c1", "s1")

    started, delta = n.normalize(event("agent_message_chunk", {"text": "hi"}))
    completed = n.normalize(event("session.idle", {"usage": {"input_tokens": 1}}))[0]

    assert started.type == "turn.started"
    assert delta.type == "content.delta"
    assert delta.turn_id == started.turn_id
    assert delta.payload == {"stream_kind": "assistant_text", "text": "hi"}
    assert completed.type == "turn.completed"
    assert completed.payload["status"] == "succeeded"


def test_opencode_idle_without_prior_activity_still_emits_turn_boundary():
    n = OpenCodeNormalizer("c1", "s1")

    started, completed = n.normalize(
        event(
            "session.idle",
            {
                "turn_id": "idle-1",
                "duration_ms": 7,
                "usage": {"output_tokens": 3},
            },
        )
    )

    assert started.type == "turn.started"
    assert started.turn_id == "idle-1"
    assert completed.type == "turn.completed"
    assert completed.turn_id == "idle-1"
    assert completed.payload == {
        "status": "succeeded",
        "usage": {"output_tokens": 3},
        "duration_ms": 7,
    }


def test_opencode_tool_and_file_sequences_preserve_single_turn_and_nested_files():
    n = OpenCodeNormalizer("c1", "s1")

    started, reasoning = n.normalize(event("agent_thought_chunk", {"text": "plan"}))
    item = n.normalize(
        event("tool_call", {"tool_call": {"id": "i1", "type": "bash", "name": "shell"}})
    )[0]
    updated, files = n.normalize(
        event(
            "tool_call_update",
            {
                "item_id": "i1",
                "properties": {"path": "a.txt", "operation": "write"},
            },
        )
    )

    assert started.type == "turn.started"
    assert reasoning.turn_id == started.turn_id
    assert reasoning.payload == {"stream_kind": "reasoning_text", "text": "plan"}
    assert item.type == "item.started"
    assert item.turn_id == started.turn_id
    assert item.payload["item_type"] == "command_execution"
    assert updated.type == "item.updated"
    assert updated.turn_id == started.turn_id
    assert files.type == "files.persisted"
    assert files.payload == {"files": [{"path": "a.txt", "operation": "write"}]}


def test_opencode_session_error_and_request_events_keep_no_runtime_hitl_explicit():
    n = OpenCodeNormalizer("c1", "s1")

    started, reasoning = n.normalize(event("agent_thought_chunk", {"text": "hmm"}))
    runtime_error, completed = n.normalize(event("session.error", {"error": "boom"}))

    assert started.type == "turn.started"
    assert reasoning.payload == {"stream_kind": "reasoning_text", "text": "hmm"}
    assert runtime_error.type == "runtime.error"
    assert runtime_error.payload == {"error": "boom", "supports_runtime_hitl": False}
    assert completed.type == "turn.completed"
    assert completed.turn_id == started.turn_id
    assert completed.payload == {"status": "error", "error": "boom"}
    assert n.normalize(event("request.opened", {"request_id": "r1"})) == []
    assert n.normalize(event("user_input.requested", {"request_id": "r2"})) == []
    assert n.normalize(event("surprise", {})) == []
