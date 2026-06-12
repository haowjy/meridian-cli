"""Tests for opencode report extraction."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.common import extract_opencode_report
from meridian.lib.harness.extractors.opencode import OPENCODE_EXTRACTOR
from meridian.lib.harness.opencode_storage import resolve_opencode_storage_root
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.launch.report import extract_or_fallback_report


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


def test_extract_opencode_report_reads_assistant_text_from_message_part_updated() -> None:
    """Regression for p2488: assistant text in message.part.updated, not info.parts."""

    spawn_id = SpawnId("p-opencode-part-updated")
    assistant_message_id = "msg_assistant_live"
    store = _artifact_store_from_history_lines(
        spawn_id,
        [
            {
                "byte_offset": 0,
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_user",
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": "msg_user",
                            "role": "user",
                            "sessionID": "ses_p2488",
                            "parts": [{"type": "text", "text": "Reply LIVE_OK only"}],
                        }
                    },
                },
                "seq": 1,
            },
            {
                "byte_offset": 50,
                "event_type": "message.updated",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_assistant_meta",
                    "type": "message.updated",
                    "properties": {
                        "info": {
                            "id": assistant_message_id,
                            "role": "assistant",
                            "sessionID": "ses_p2488",
                        }
                    },
                },
                "seq": 2,
            },
            {
                "byte_offset": 100,
                "event_type": "message.part.updated",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_assistant_part",
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "ses_p2488",
                        "part": {
                            "id": "prt_live",
                            "messageID": assistant_message_id,
                            "sessionID": "ses_p2488",
                            "type": "text",
                            "text": "LIVE_OK",
                        },
                    },
                },
                "seq": 3,
            },
            {
                "byte_offset": 150,
                "event_type": "session.idle",
                "harness_id": "opencode",
                "payload": {
                    "id": "evt_idle",
                    "type": "session.idle",
                    "properties": {"sessionID": "ses_p2488"},
                },
                "seq": 4,
            },
        ],
    )

    assert extract_opencode_report(store, spawn_id) == "LIVE_OK"


def test_extract_or_fallback_report_never_returns_session_idle_envelope() -> None:
    spawn_id = SpawnId("p-opencode-fallback-idle")
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
                    "properties": {"sessionID": "ses_idle_only"},
                },
                "seq": 1,
            }
        ],
    )

    report = extract_or_fallback_report(store, spawn_id, extractor=OPENCODE_EXTRACTOR)

    assert report.content is None
    assert "session.idle" not in (report.content or "")


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

    db_path = tmp_path / "opencode" / "opencode.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO message "
            "(id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            ("msg_1", session_id, 1, 1, json.dumps({"role": "assistant"})),
        )
        connection.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_1",
                "msg_1",
                session_id,
                1,
                1,
                json.dumps({"type": "text", "text": "LIVE_OK"}),
            ),
        )
        connection.commit()

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

    db_path = tmp_path / "opencode" / "opencode.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO message "
            "(id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            ("msg_1", session_id, 1, 1, json.dumps({"role": "assistant"})),
        )
        connection.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_1",
                "msg_1",
                session_id,
                1,
                1,
                json.dumps({"type": "text", "text": "FINAL_OK"}),
            ),
        )
        connection.execute(
            "INSERT INTO message "
            "(id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "msg_2",
                session_id,
                2,
                2,
                json.dumps(
                    {
                        "role": "assistant",
                        "mode": "compaction",
                        "agent": "compaction",
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_2",
                "msg_2",
                session_id,
                2,
                2,
                json.dumps({"type": "text", "text": "handoff only"}),
            ),
        )
        connection.commit()

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
