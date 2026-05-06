from meridian.lib.chat.normalization.registry import get_normalizer_factory
from meridian.lib.harness.connections.base import HarnessEvent


def _event(harness_id: str, event_type: str, payload: dict[str, object]) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, payload=payload, harness_id=harness_id)


def _types_for(harness_id: str, events: list[HarnessEvent]) -> set[str]:
    normalizer = get_normalizer_factory(harness_id)("chat-1", "exec-1")
    types: set[str] = set()
    for event in events:
        types.update(chat_event.type for chat_event in normalizer.normalize(event))
    return types


def test_all_harnesses_emit_core_chat_event_families_without_server_branching():
    cases = {
        "claude": [
            _event(
                "claude",
                "assistant",
                {"message": {"content": [{"type": "text", "text": "hi"}]}},
            ),
            _event("claude", "result", {"status": "succeeded"}),
        ],
        "codex": [
            _event("codex", "turn/started", {"turn": {"id": "t1"}}),
            _event(
                "codex",
                "item/agentMessage/delta",
                {"turnId": "t1", "itemId": "m1", "delta": "hi"},
            ),
            _event("codex", "turn/completed", {"turn": {"id": "t1", "status": "completed"}}),
        ],
        "opencode": [
            _event(
                "opencode",
                "message.updated",
                {
                    "properties": {
                        "info": {"id": "msg-1", "role": "assistant"},
                        "message": {
                            "parts": [
                                {
                                    "id": "p1",
                                    "messageID": "msg-1",
                                    "type": "text",
                                    "text": "hi",
                                }
                            ]
                        },
                    }
                },
            ),
            _event("opencode", "session.idle", {}),
        ],
    }

    for harness_id, events in cases.items():
        assert {"turn.started", "content.delta", "turn.completed"} <= _types_for(
            harness_id, events
        )


def test_all_harnesses_emit_canonical_files_persisted():
    for harness_id in ("claude", "codex", "opencode"):
        types = _types_for(harness_id, [_event(harness_id, "files.persisted", {"path": "a.txt"})])
        assert "files.persisted" in types


def test_all_harnesses_emit_item_lifecycle_for_tool_activity():
    cases = {
        "claude": [
            _event(
                "claude",
                "assistant",
                {
                    "message": {
                        "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read"}]
                    }
                },
            ),
            _event(
                "claude",
                "user",
                {
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
                        ]
                    }
                },
            ),
        ],
        "codex": [
            _event("codex", "turn/started", {"turn": {"id": "t1"}}),
            _event(
                "codex",
                "item/started",
                {
                    "turnId": "t1",
                    "item": {"id": "call-1", "type": "commandExecution", "name": "bash"},
                },
            ),
            _event(
                "codex",
                "item/completed",
                {"turnId": "t1", "item": {"id": "call-1", "type": "commandExecution"}},
            ),
        ],
        "opencode": [
            _event(
                "opencode",
                "message.part.updated",
                {
                    "properties": {
                        "part": {
                            "id": "prt-1",
                            "type": "tool",
                            "tool": "bash",
                            "callID": "bash:0",
                            "state": {"status": "pending"},
                        }
                    }
                },
            ),
            _event(
                "opencode",
                "message.part.updated",
                {
                    "properties": {
                        "part": {
                            "id": "prt-1",
                            "type": "tool",
                            "tool": "bash",
                            "callID": "bash:0",
                            "state": {"status": "completed"},
                        }
                    }
                },
            ),
        ],
    }

    for harness_id, events in cases.items():
        assert {"item.started", "item.completed"} <= _types_for(harness_id, events)


def test_unknown_events_drop_instead_of_crashing():
    for harness_id in ("claude", "codex", "opencode"):
        assert _types_for(harness_id, [_event(harness_id, "unknown.event", {})]) == set()
