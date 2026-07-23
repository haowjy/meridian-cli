"""OpenCode artifact, database, and legacy-session report fallbacks."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.extractors.opencode import OPENCODE_EXTRACTOR
from meridian.lib.harness.opencode_report import extract_opencode_report
from meridian.lib.harness.opencode_storage import resolve_opencode_storage_root
from meridian.lib.launch.constants import HISTORY_FILENAME
from tests.support.opencode_db import write_opencode_db_session_with_parts


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

def test_extract_opencode_report_ignores_child_session_assistant_text() -> None:
    spawn_id = SpawnId("p-opencode-parent-scope")
    child_message_id = "msg_child_assistant"
    parent_message_id = "msg_parent_assistant"
    store = _artifact_store_from_history_lines(
        spawn_id,
        [
            {
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": "msg_parent_user",
                            "role": "user",
                            "sessionID": "ses_parent",
                            "parts": [{"type": "text", "text": "Parent task"}],
                        }
                    },
                },
            },
            {
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": "msg_child_user",
                            "role": "user",
                            "sessionID": "ses_child",
                            "parts": [{"type": "text", "text": "Child task"}],
                        }
                    },
                },
            },
            {
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": child_message_id,
                            "role": "assistant",
                            "sessionID": "ses_child",
                        }
                    },
                },
            },
            {
                "event_type": "message.part.updated",
                "harness_id": "opencode",
                "payload": {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "ses_child",
                        "part": {
                            "messageID": child_message_id,
                            "sessionID": "ses_child",
                            "type": "text",
                            "text": "Child report must not be parent report.",
                        },
                    },
                },
            },
            {
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": parent_message_id,
                            "role": "assistant",
                            "sessionID": "ses_parent",
                        }
                    },
                },
            },
            {
                "event_type": "message.part.updated",
                "harness_id": "opencode",
                "payload": {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "ses_parent",
                        "part": {
                            "messageID": parent_message_id,
                            "sessionID": "ses_parent",
                            "type": "text",
                            "text": "Parent report.",
                        },
                    },
                },
            },
        ],
    )

    assert OPENCODE_EXTRACTOR.extract_session_id(store, spawn_id) == "ses_parent"
    assert extract_opencode_report(store, spawn_id) == "Parent report."

def test_extract_opencode_report_falls_back_to_opencode_db_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_id = "ses_fixture_report_db"
    spawn_id = SpawnId("p-opencode-db-fallback")
    storage_root = tmp_path / "opencode" / "storage"
    session_file = storage_root / "session_diff" / f"{session_id}.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("[]\n", encoding="utf-8")

    write_opencode_db_session_with_parts(
        db_path=tmp_path / "opencode" / "opencode.db",
        session_id=session_id,
        messages=[
            ("assistant", {}, [{"type": "text", "text": "LIVE_OK"}]),
        ],
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert resolve_opencode_storage_root() == storage_root

    store = _artifact_store_from_history_lines(
        spawn_id,
        [
            {
                "byte_offset": 0,
                "event_type": "session.idle",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_idle",
                    "type": "session.idle",
                    "properties": {"sessionID": session_id},
                },
                "seq": 1,
            }
        ],
    )
    store._payloads[f"{spawn_id}/session_id.txt"] = session_id.encode("utf-8")

    assert extract_opencode_report(store, spawn_id) == "LIVE_OK"

def test_extract_opencode_report_ignores_opencode_db_compaction_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_id = "ses_fixture_report_db_compaction"
    spawn_id = SpawnId("p-opencode-db-compaction")
    storage_root = tmp_path / "opencode" / "storage"
    session_file = storage_root / "session_diff" / f"{session_id}.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("[]\n", encoding="utf-8")

    write_opencode_db_session_with_parts(
        db_path=tmp_path / "opencode" / "opencode.db",
        session_id=session_id,
        messages=[
            ("assistant", {}, [{"type": "text", "text": "FINAL_OK"}]),
            (
                "assistant",
                {"mode": "compaction", "agent": "compaction"},
                [{"type": "text", "text": "handoff only"}],
            ),
        ],
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert resolve_opencode_storage_root() == storage_root

    store = _artifact_store_from_history_lines(
        spawn_id,
        [
            {
                "byte_offset": 0,
                "event_type": "session.idle",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_idle",
                    "type": "session.idle",
                    "properties": {"sessionID": session_id},
                },
                "seq": 1,
            }
        ],
    )
    store._payloads[f"{spawn_id}/session_id.txt"] = session_id.encode("utf-8")

    assert extract_opencode_report(store, spawn_id) == "FINAL_OK"
