from meridian.lib.chat.normalization.opencode import OpenCodeNormalizer
from meridian.lib.harness.connections.base import HarnessEvent


def event(
    event_type: str, payload: dict[str, object], harness_id: str = "opencode"
) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, payload=payload, harness_id=harness_id)


def test_opencode_live_shapes_emit_text_reasoning_and_tool_lifecycle_once():
    n = OpenCodeNormalizer("c1", "s1")

    busy_started = n.normalize(
        event(
            "session.status",
            {"properties": {"sessionID": "ses-1", "status": {"type": "busy"}}},
        )
    )[0]
    n.normalize(
        event(
            "message.part.updated",
            {"properties": {"part": {"id": "p-r", "type": "reasoning", "text": "plan"}}},
        )
    )
    reasoning_delta = n.normalize(
        event(
            "message.part.delta",
            {"properties": {"partID": "p-r", "field": "text", "delta": " now"}},
        )
    )[0]
    n.normalize(
        event(
            "message.part.updated",
            {"properties": {"part": {"id": "p-t", "type": "text", "text": "OK"}}},
        )
    )
    text_delta = n.normalize(
        event(
            "message.part.delta",
            {"properties": {"partID": "p-t", "field": "text", "delta": "!"}},
        )
    )[0]
    tool_started = n.normalize(
        event(
            "message.part.updated",
            {
                "properties": {
                    "part": {
                        "id": "prt-tool",
                        "type": "tool",
                        "tool": "bash",
                        "callID": "bash:0",
                        "state": {"status": "pending", "input": {"command": "pwd"}},
                    }
                }
            },
        )
    )[0]
    tool_updated = n.normalize(
        event(
            "message.part.updated",
            {
                "properties": {
                    "part": {
                        "id": "prt-tool",
                        "type": "tool",
                        "tool": "bash",
                        "callID": "bash:0",
                        "state": {
                            "status": "running",
                            "input": {"command": "pwd"},
                            "metadata": {"output": "/repo\n"},
                        },
                    }
                }
            },
        )
    )[0]
    tool_completed = n.normalize(
        event(
            "message.part.updated",
            {
                "properties": {
                    "part": {
                        "id": "prt-tool",
                        "type": "tool",
                        "tool": "bash",
                        "callID": "bash:0",
                        "state": {
                            "status": "completed",
                            "input": {"command": "pwd"},
                            "output": "/repo\n",
                            "metadata": {"exit": 0},
                        },
                    }
                }
            },
        )
    )[0]
    turn_completed = n.normalize(event("session.idle", {"info": {"input_tokens": 1}}))[0]
    synthetic = n.normalize(event("meridian/turn_completed", {"synthetic": True}, "meridian"))

    assert busy_started.type == "turn.started"
    assert busy_started.payload == {"session_id": "ses-1"}
    assert reasoning_delta.type == "content.delta"
    assert reasoning_delta.payload == {"stream_kind": "reasoning_text", "text": " now"}
    assert text_delta.type == "content.delta"
    assert text_delta.payload == {"stream_kind": "assistant_text", "text": "!"}
    assert tool_started.type == "item.started"
    assert tool_started.item_id == "bash:0"
    assert tool_updated.type == "item.updated"
    assert tool_updated.item_id == "bash:0"
    assert tool_completed.type == "item.completed"
    assert tool_completed.item_id == "bash:0"
    assert turn_completed.type == "turn.completed"
    assert turn_completed.payload == {"status": "succeeded", "usage": {"input_tokens": 1}}
    assert synthetic == []


def test_opencode_message_updated_snapshot_backfills_missing_output_and_terminal_tool_state():
    n = OpenCodeNormalizer("c1", "s1")

    events = n.normalize(
        event(
            "message.updated",
            {
                "properties": {
                    "message": {
                        "parts": [
                            {"id": "text-1", "type": "text", "text": "snapshot text"},
                            {
                                "id": "tool-1",
                                "type": "tool",
                                "tool": "bash",
                                "callID": "bash:2",
                                "state": {
                                    "status": "completed",
                                    "input": {"command": "pwd"},
                                    "output": "/repo\n",
                                    "metadata": {"exit": 0},
                                },
                            },
                        ]
                    }
                }
            },
        )
    )

    assert [entry.type for entry in events] == [
        "turn.started",
        "content.delta",
        "item.started",
        "item.completed",
    ]
    assert events[1].payload == {"stream_kind": "assistant_text", "text": "snapshot text"}
    assert events[3].item_id == "bash:2"


def test_opencode_legacy_shapes_still_work():
    n = OpenCodeNormalizer("c1", "s1")

    started, text = n.normalize(event("agent_message_chunk", {"text": "hi"}))
    thought = n.normalize(event("agent_thought_chunk", {"text": "plan"}))[0]
    item = n.normalize(
        event("tool_call", {"tool_call": {"id": "i1", "type": "bash", "name": "shell"}})
    )[0]
    runtime_error, completed = n.normalize(event("session.error", {"error": "boom"}))

    assert started.type == "turn.started"
    assert text.type == "content.delta"
    assert text.payload == {"stream_kind": "assistant_text", "text": "hi"}
    assert thought.payload == {"stream_kind": "reasoning_text", "text": "plan"}
    assert item.type == "item.started"
    assert runtime_error.type == "runtime.error"
    assert completed.type == "turn.completed"
    assert completed.payload == {"status": "error", "error": "boom"}
