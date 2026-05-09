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
            "message.updated",
            {"properties": {"info": {"id": "msg-a1", "role": "assistant"}}},
        )
    )
    n.normalize(
        event(
            "message.part.updated",
            {
                "properties": {
                    "part": {
                        "id": "p-r",
                        "messageID": "msg-a1",
                        "type": "reasoning",
                        "text": "plan",
                    }
                }
            },
        )
    )
    reasoning_delta = n.normalize(
        event(
            "message.part.delta",
            {
                "properties": {
                    "partID": "p-r",
                    "messageID": "msg-a1",
                    "field": "text",
                    "delta": " now",
                }
            },
        )
    )[0]
    n.normalize(
        event(
            "message.part.updated",
            {
                "properties": {
                    "part": {
                        "id": "p-t",
                        "messageID": "msg-a1",
                        "type": "text",
                        "text": "OK",
                    }
                }
            },
        )
    )
    text_delta = n.normalize(
        event(
            "message.part.delta",
            {
                "properties": {
                    "partID": "p-t",
                    "messageID": "msg-a1",
                    "field": "text",
                    "delta": "!",
                }
            },
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
    assert turn_completed.payload == {"status": "succeeded"}
    assert synthetic == []


def test_opencode_message_updated_snapshot_backfills_missing_output_and_terminal_tool_state():
    n = OpenCodeNormalizer("c1", "s1")

    events = n.normalize(
        event(
            "message.updated",
            {
                "properties": {
                    "info": {"id": "msg-a2", "role": "assistant"},
                    "message": {
                        "parts": [
                            {
                                "id": "text-1",
                                "messageID": "msg-a2",
                                "type": "text",
                                "text": "snapshot text",
                            },
                            {
                                "id": "tool-1",
                                "type": "tool",
                                "tool": "bash",
                                "messageID": "msg-a2",
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


def test_opencode_authoritative_snapshot_does_not_duplicate_streamed_text_or_completed_tool():
    n = OpenCodeNormalizer("c1", "s1")

    n.normalize(
        event(
            "message.updated",
            {"properties": {"info": {"id": "msg-a3", "role": "assistant"}}},
        )
    )
    n.normalize(
        event(
            "message.part.updated",
            {
                "properties": {
                    "part": {
                        "id": "text-1",
                        "messageID": "msg-a3",
                        "type": "text",
                        "text": "",
                    }
                }
            },
        )
    )
    text_delta_events = n.normalize(
        event(
            "message.part.delta",
            {
                "properties": {
                    "partID": "text-1",
                    "messageID": "msg-a3",
                    "field": "text",
                    "delta": "Hello",
                }
            },
        )
    )
    tool_started = n.normalize(
        event(
            "message.part.updated",
            {
                "properties": {
                    "part": {
                        "id": "tool-1",
                        "messageID": "msg-a3",
                        "type": "tool",
                        "tool": "bash",
                        "callID": "bash:3",
                        "state": {"status": "pending", "input": {"command": "pwd"}},
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
                        "id": "tool-1",
                        "messageID": "msg-a3",
                        "type": "tool",
                        "tool": "bash",
                        "callID": "bash:3",
                        "state": {
                            "status": "completed",
                            "input": {"command": "pwd"},
                            "output": "/tmp\n",
                            "metadata": {"exit": 0},
                        },
                    }
                }
            },
        )
    )[-1]

    snapshot = n.normalize(
        event(
            "message.updated",
            {
                "properties": {
                    "info": {"id": "msg-a3", "role": "assistant"},
                    "message": {
                        "parts": [
                            {
                                "id": "text-1",
                                "messageID": "msg-a3",
                                "type": "text",
                                "text": "Hello",
                            },
                            {
                                "id": "tool-1",
                                "messageID": "msg-a3",
                                "type": "tool",
                                "tool": "bash",
                                "callID": "bash:3",
                                "state": {
                                    "status": "completed",
                                    "input": {"command": "pwd"},
                                    "output": "/tmp\n",
                                    "metadata": {"exit": 0},
                                },
                            },
                        ]
                    },
                }
            },
        )
    )

    assert [entry.type for entry in text_delta_events] == ["turn.started", "content.delta"]
    assert text_delta_events[1].payload == {"stream_kind": "assistant_text", "text": "Hello"}
    assert tool_started.type == "item.started"
    assert tool_completed.type == "item.completed"
    assert tool_completed.item_id == "bash:3"
    assert snapshot == []


def test_opencode_session_idle_without_active_turn_is_noop():
    n = OpenCodeNormalizer("c1", "s1")

    assert n.normalize(event("session.idle", {})) == []


def test_opencode_user_message_parts_do_not_emit_assistant_content_delta():
    n = OpenCodeNormalizer("c1", "s1")

    n.normalize(
        event(
            "message.updated",
            {
                "properties": {
                    "info": {
                        "id": "msg-user-1",
                        "role": "user",
                    }
                }
            },
        )
    )
    snapshot_events = n.normalize(
        event(
            "message.part.updated",
            {
                "properties": {
                    "part": {
                        "id": "part-user-1",
                        "messageID": "msg-user-1",
                        "type": "text",
                        "text": "user prompt",
                    }
                }
            },
        )
    )
    delta_events = n.normalize(
        event(
            "message.part.delta",
            {
                "properties": {
                    "partID": "part-user-1",
                    "messageID": "msg-user-1",
                    "field": "text",
                    "delta": " user prompt",
                }
            },
        )
    )

    assert snapshot_events == []
    assert delta_events == []


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


def test_opencode_current_schema_turn_completed_uses_message_updated_usage_cache():
    n = OpenCodeNormalizer("c1", "s1")

    n.normalize(event("session.status", {"properties": {"status": {"type": "busy"}}}))
    n.normalize(
        event(
            "message.updated",
            {
                "properties": {
                    "info": {
                        "id": "msg-a4",
                        "role": "assistant",
                        "tokens": {
                            "input": 12,
                            "output": 4,
                            "reasoning": 2,
                            "cache": {"read": 6, "write": 3},
                        },
                        "cost": 0.05,
                    }
                }
            },
        )
    )

    completed = n.normalize(event("session.idle", {"properties": {"status": "idle"}}))[0]

    assert completed.type == "turn.completed"
    assert completed.payload == {
        "status": "succeeded",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 4,
            "cache_read_input_tokens": 6,
            "cache_creation_input_tokens": 3,
            "reasoning_tokens": 2,
            "total_cost_usd": 0.05,
        },
    }


def test_opencode_session_idle_info_does_not_override_message_updated_usage_cache():
    n = OpenCodeNormalizer("c1", "s1")

    n.normalize(event("session.status", {"properties": {"status": {"type": "busy"}}}))
    n.normalize(
        event(
            "message.updated",
            {
                "properties": {
                    "info": {
                        "id": "msg-a6",
                        "role": "assistant",
                        "tokens": {"input": 7, "output": 2},
                    }
                }
            },
        )
    )

    completed = n.normalize(
        event(
            "session.idle",
            {
                "properties": {
                    "status": "idle",
                    "info": {"tokens": {"input": 999, "output": 999}},
                }
            },
        )
    )[0]

    assert completed.payload == {
        "status": "succeeded",
        "usage": {"input_tokens": 7, "output_tokens": 2},
    }


def test_opencode_turn_usage_cache_resets_between_turns():
    n = OpenCodeNormalizer("c1", "s1")

    n.normalize(event("session.status", {"properties": {"status": {"type": "busy"}}}))
    n.normalize(
        event(
            "message.updated",
            {
                "properties": {
                    "info": {
                        "id": "msg-a5",
                        "role": "assistant",
                        "tokens": {"input": 5, "output": 1},
                    }
                }
            },
        )
    )
    first = n.normalize(event("session.idle", {"properties": {"status": "idle"}}))[0]
    n.normalize(event("session.status", {"properties": {"status": {"type": "busy"}}}))
    second = n.normalize(event("session.idle", {"properties": {"status": "idle"}}))[0]

    assert first.payload == {
        "status": "succeeded",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }
    assert second.payload == {"status": "succeeded"}
