from meridian.lib.chat.normalization.codex import CodexNormalizer
from meridian.lib.harness.connections.base import HarnessEvent


def event(event_type: str, payload: dict[str, object]) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, payload=payload, harness_id="codex")


def test_codex_maps_live_item_shapes_and_nested_ids_without_duplicate_completion():
    n = CodexNormalizer("c1", "s1")

    started = n.normalize(
        event(
            "turn/started",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "inProgress"},
            },
        )
    )[0]
    tool_started = n.normalize(
        event(
            "item/started",
            {
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "id": "call-1", "command": "pwd"},
            },
        )
    )[0]
    tool_completed = n.normalize(
        event(
            "item/completed",
            {
                "turnId": "turn-1",
                "item": {
                    "type": "commandExecution",
                    "id": "call-1",
                    "aggregatedOutput": "/repo\n",
                    "exitCode": 0,
                },
            },
        )
    )[0]
    n.normalize(
        event(
            "item/started",
            {
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "id": "msg-1", "text": ""},
            },
        )
    )
    delta = n.normalize(
        event(
            "item/agentMessage/delta",
            {"turnId": "turn-1", "itemId": "msg-1", "delta": "OK"},
        )
    )[0]
    message_completed = n.normalize(
        event(
            "item/completed",
            {
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "id": "msg-1", "text": "OK"},
            },
        )
    )[0]
    completed = n.normalize(
        event(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed", "error": None, "durationMs": 12},
            },
        )
    )[0]
    synthetic = n.normalize(
        event("meridian/turn_completed", {"status": "succeeded", "synthetic": True})
    )

    assert started.type == "turn.started"
    assert started.turn_id == "turn-1"
    assert started.payload == {"thread_id": "thread-1"}
    assert tool_started.type == "item.started"
    assert tool_started.item_id == "call-1"
    assert tool_started.payload["item_type"] == "command_execution"
    assert tool_completed.type == "item.completed"
    assert tool_completed.item_id == "call-1"
    assert delta.type == "content.delta"
    assert delta.turn_id == "turn-1"
    assert delta.item_id == "msg-1"
    assert delta.payload == {"stream_kind": "assistant_text", "text": "OK"}
    assert message_completed.type == "item.completed"
    assert message_completed.item_id == "msg-1"
    assert completed.type == "turn.completed"
    assert completed.turn_id == "turn-1"
    assert completed.payload == {"status": "completed", "duration_ms": 12, "thread_id": "thread-1"}
    assert synthetic == []


def test_codex_emits_terminal_assistant_text_from_item_completed_when_delta_missing():
    n = CodexNormalizer("c1", "s1")
    n.normalize(event("turn/started", {"turn_id": "t1"}))

    item_completed, text = n.normalize(
        event(
            "item/completed",
            {"item": {"type": "agentMessage", "id": "msg-2", "text": "fallback"}},
        )
    )

    assert item_completed.type == "item.completed"
    assert item_completed.item_id == "msg-2"
    assert text.type == "content.delta"
    assert text.item_id == "msg-2"
    assert text.payload == {"stream_kind": "assistant_text", "text": "fallback"}


def test_codex_stale_duplicate_completion_does_not_close_newer_turn():
    n = CodexNormalizer("c1", "s1")

    first_started = n.normalize(event("turn/started", {"turn": {"id": "t1"}}))[0]
    first_completed = n.normalize(event("turn/completed", {"turn": {"id": "t1"}}))[0]
    second_started = n.normalize(event("turn/started", {"turn": {"id": "t2"}}))[0]

    stale_completion = n.normalize(event("turn/completed", {"turn": {"id": "t1"}}))
    second_delta = n.normalize(
        event("item/agentMessage/delta", {"turnId": "t2", "itemId": "msg-2", "delta": "hi"})
    )[0]
    second_completed = n.normalize(event("turn/completed", {"turn": {"id": "t2"}}))[0]

    assert first_started.turn_id == "t1"
    assert first_completed.turn_id == "t1"
    assert second_started.turn_id == "t2"
    assert stale_completion == []
    assert second_delta.turn_id == "t2"
    assert second_completed.turn_id == "t2"


def test_codex_preserves_legacy_event_shapes():
    n = CodexNormalizer("c1", "s1")

    started = n.normalize(event("turn/started", {"turn_id": "t1", "model": "gpt"}))[0]
    delta = n.normalize(event("agent_message_chunk", {"text": "hi"}))[0]
    thought = n.normalize(event("agent_thought_chunk", {"text": "hmm"}))[0]
    completed = n.normalize(event("turn/completed", {"usage": {"input_tokens": 1}}))[0]

    assert started.type == "turn.started"
    assert started.turn_id == "t1"
    assert started.payload == {"model": "gpt"}
    assert delta.type == "content.delta"
    assert delta.payload == {"stream_kind": "assistant_text", "text": "hi"}
    assert thought.type == "content.delta"
    assert thought.payload == {"stream_kind": "reasoning_text", "text": "hmm"}
    assert completed.type == "turn.completed"
    assert completed.payload["usage"] == {"input_tokens": 1}


def test_codex_request_and_user_input_events_preserve_payload_fidelity():
    n = CodexNormalizer("c1", "s1")

    opened = n.normalize(
        event(
            "request/opened",
            {
                "id": "r1",
                "request_type": "approval",
                "method": "apply_patch",
                "params": {"path": "a.txt"},
            },
        )
    )[0]
    resolved = n.normalize(
        event("request.resolved", {"request_id": "r1", "decision": "accept"})
    )[0]
    user_input = n.normalize(
        event("user_input/requested", {"request_id": "r2", "questions": [{"id": "q1"}]})
    )[0]

    assert opened.type == "request.opened"
    assert opened.request_id == "r1"
    assert opened.payload["method"] == "apply_patch"
    assert resolved.type == "request.resolved"
    assert resolved.request_id == "r1"
    assert resolved.payload["decision"] == "accept"
    assert user_input.type == "user_input.requested"
    assert user_input.request_id == "r2"
    assert user_input.payload["request_type"] == "user_input"
