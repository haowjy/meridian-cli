"""Tests for opencode report extraction."""

from __future__ import annotations

import json

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.common import extract_opencode_report
from meridian.lib.launch.constants import HISTORY_FILENAME


class _MemoryArtifactStore:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads

    def get(self, key: ArtifactKey) -> bytes:
        return self._payloads[str(key)]

    def exists(self, key: ArtifactKey) -> bool:
        return str(key) in self._payloads


def _artifact_store_from_history_lines(
    spawn_id: SpawnId, lines: list[dict[str, object]]
) -> _MemoryArtifactStore:
    encoded = "\n".join(json.dumps(line) for line in lines).encode("utf-8")
    return _MemoryArtifactStore({f"{spawn_id}/{HISTORY_FILENAME}": encoded})


def test_extract_opencode_report_reads_last_assistant_text_from_wrapped_history() -> None:
    spawn_id = SpawnId("p-opencode-report")
    store = _artifact_store_from_history_lines(
        spawn_id,
        [
            {
                "byte_offset": 0,
                "event_type": "session.idle",
                "harness_id": "opencode",
                "payload": {"id": "evt_idle", "type": "session.idle", "properties": {}},
                "seq": 1,
            },
            {
                "byte_offset": 50,
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_user",
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": "msg_user",
                            "role": "user",
                            "sessionID": "ses_test",
                            "parts": [{"type": "text", "text": "Reply with exactly OK"}],
                        }
                    },
                },
                "seq": 2,
            },
            {
                "byte_offset": 100,
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_assistant_partial",
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": "msg_assistant",
                            "role": "assistant",
                            "sessionID": "ses_test",
                            "parts": [
                                {"type": "text", "text": "O"},
                                {"type": "tool_call", "name": "shell"},
                            ],
                        }
                    },
                },
                "seq": 3,
            },
            {
                "byte_offset": 120,
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_assistant_final",
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": "msg_assistant",
                            "role": "assistant",
                            "sessionID": "ses_test",
                            "parts": [
                                {"type": "text", "text": "OK"},
                                {"type": "reasoning", "text": "ignored"},
                            ],
                        }
                    },
                },
                "seq": 4,
            },
            {
                "byte_offset": 150,
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_assistant_notext",
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": "msg_assistant",
                            "role": "assistant",
                            "sessionID": "ses_test",
                            "parts": [{"type": "tool_call", "name": "shell"}],
                        }
                    },
                },
                "seq": 5,
            },
        ],
    )

    assert extract_opencode_report(store, spawn_id) == "OK"


def test_extract_opencode_report_returns_none_when_assistant_text_is_missing() -> None:
    spawn_id = SpawnId("p-opencode-no-assistant")
    store = _artifact_store_from_history_lines(
        spawn_id,
        [
            {
                "byte_offset": 0,
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_user_only",
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": "msg_user",
                            "role": "user",
                            "sessionID": "ses_test",
                            "parts": [{"type": "text", "text": "hello"}],
                        }
                    },
                },
                "seq": 1,
            }
        ],
    )

    assert extract_opencode_report(store, spawn_id) is None
