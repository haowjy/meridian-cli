from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

import meridian.lib.chat.server as server
from meridian.lib.chat.event_log import ChatEventLog
from meridian.lib.chat.protocol import ChatEvent, utc_now_iso
from meridian.lib.chat.server import app, configure
from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import RuntimePaths


class Handle:
    spawn_id = SpawnId("p-recovery")

    def health(self) -> bool:
        return True

    async def send_message(self, text: str) -> None:
        pass

    async def send_cancel(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class Acquisition:
    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ) -> Handle:
        _ = (chat_id, initial_prompt, execution_generation, chat_state)
        return Handle()


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths.from_root_dir(tmp_path)


def _history_path(tmp_path: Path, chat_id: str) -> Path:
    return _paths(tmp_path).chat_history_path(chat_id)


def _index_path(tmp_path: Path, chat_id: str) -> Path:
    return _paths(tmp_path).chats_dir / chat_id / "index.sqlite3"


def _ingest_turn_started(
    client: TestClient,
    chat_id: str,
    execution_id: str = "p-recovery",
) -> None:
    pipeline = server._runtime.live_entries[chat_id].pipeline
    client.portal.call(
        pipeline.ingest,
        ChatEvent(
            type="turn.started",
            seq=0,
            chat_id=chat_id,
            execution_id=execution_id,
            timestamp=utc_now_iso(),
        ),
    )
    client.portal.call(pipeline.drain)


def _event_types(client: TestClient, chat_id: str, count: int) -> list[str]:
    stream_source = server._runtime.get_stream_source(chat_id)
    assert stream_source is not None
    expected_event_count = len(list(stream_source.event_log.read_all()))
    with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
        events = [ws.receive_json() for _ in range(expected_event_count)]
    relevant_types = [
        event["type"]
        for event in events
        if event["type"] not in {"user.prompt", "chat.state_changed"}
    ]
    return relevant_types[:count]


def _replayed_events(client: TestClient, chat_id: str) -> list[dict[str, object]]:
    stream_source = server._runtime.get_stream_source(chat_id)
    assert stream_source is not None
    expected_event_count = len(list(stream_source.event_log.read_all()))
    with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
        return [ws.receive_json() for _ in range(expected_event_count)]


def _state(client: TestClient, chat_id: str) -> str:
    return client.get(f"/chat/{chat_id}/state").json()["state"]


def _runtime_error_count(tmp_path: Path, chat_id: str) -> int:
    return sum(
        1
        for event in ChatEventLog(_history_path(tmp_path, chat_id)).read_all()
        if event.type == "runtime.error"
    )


def test_restart_recovery_restores_non_closed_chat_to_idle_and_emits_runtime_error(
    tmp_path: Path,
) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        assert (
            client.post(f"/chat/{chat_id}/msg", json={"text": "hi"}).json()["status"]
            == "accepted"
        )
        _ingest_turn_started(client, chat_id)

    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        assert client.get(f"/chat/{chat_id}/state").json() == {"chat_id": chat_id, "state": "idle"}
        events = _replayed_events(client, chat_id)

    relevant_events = [
        event
        for event in events
        if event["type"] not in {"user.prompt", "chat.state_changed"}
    ]
    assert [event["type"] for event in relevant_events] == [
        "chat.started",
        "turn.started",
        "runtime.error",
    ]
    assert relevant_events[-1]["payload"]["reason"] == "backend_lost_after_restart"


def test_restart_recovery_tolerates_truncated_jsonl_tail(tmp_path: Path) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]

    with _history_path(tmp_path, chat_id).open("ab") as handle:
        handle.write(b'{"type":"turn.started"')

    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        assert _state(client, chat_id) == "idle"
        assert _event_types(client, chat_id, 1) == ["chat.started"]


def test_restart_recovery_rebuilds_missing_or_corrupt_index_from_jsonl(
    tmp_path: Path,
) -> None:
    for mode in ("missing", "corrupt"):
        runtime_root = tmp_path / mode
        configure(runtime_root=runtime_root, backend_acquisition=Acquisition())
        with TestClient(app) as client:
            chat_id = client.post("/chat", json={}).json()["chat_id"]
            assert (
                client.post(f"/chat/{chat_id}/msg", json={"text": mode}).json()["status"]
                == "accepted"
            )
            _ingest_turn_started(client, chat_id)

        index_path = _index_path(runtime_root, chat_id)
        if mode == "missing":
            index_path.unlink()
        else:
            index_path.write_bytes(b"not a sqlite database")

        configure(runtime_root=runtime_root, backend_acquisition=Acquisition())
        with TestClient(app) as client:
            assert _state(client, chat_id) == "idle"

        conn = sqlite3.connect(index_path)
        rows = conn.execute(
            "SELECT type FROM events WHERE chat_id=? ORDER BY seq",
            (chat_id,),
        ).fetchall()
        relevant_rows = [
            row for row in rows if row[0] not in {"user.prompt", "chat.state_changed"}
        ]
        assert relevant_rows == [
            ("chat.started",),
            ("turn.started",),
            ("runtime.error",),
        ]
        conn.close()


def test_restart_recovery_handles_mixed_closed_active_and_draining_chats(
    tmp_path: Path,
) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        closed_chat = client.post("/chat", json={}).json()["chat_id"]
        assert client.post(f"/chat/{closed_chat}/close").json()["status"] == "accepted"

        active_chat = client.post("/chat", json={}).json()["chat_id"]
        assert (
            client.post(f"/chat/{active_chat}/msg", json={"text": "active"}).json()["status"]
            == "accepted"
        )
        _ingest_turn_started(client, active_chat)

        draining_chat = client.post("/chat", json={}).json()["chat_id"]
        assert (
            client.post(f"/chat/{draining_chat}/msg", json={"text": "draining"}).json()["status"]
            == "accepted"
        )
        _ingest_turn_started(client, draining_chat)
        assert client.post(f"/chat/{draining_chat}/cancel").json()["status"] == "accepted"
        assert server._runtime.live_entries[draining_chat].session.state == "draining"

    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        assert _state(client, closed_chat) == "closed"
        assert _state(client, active_chat) == "idle"
        assert _state(client, draining_chat) == "idle"

        assert closed_chat not in server._runtime.live_entries
        assert closed_chat in server._runtime.persisted_only
        assert active_chat in server._runtime.live_entries
        assert draining_chat in server._runtime.live_entries

        assert _event_types(client, closed_chat, 2) == ["chat.started", "chat.exited"]
        assert _event_types(client, active_chat, 3) == [
            "chat.started",
            "turn.started",
            "runtime.error",
        ]
        assert _event_types(client, draining_chat, 3) == [
            "chat.started",
            "turn.started",
            "runtime.error",
        ]


def test_restart_recovery_with_empty_chats_dir_is_noop(tmp_path: Path) -> None:
    assert not _paths(tmp_path).chats_dir.exists()

    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app):
        assert server._runtime.live_entries == {}
        assert server._runtime.persisted_only == {}


def test_restart_recovery_is_idempotent_and_does_not_duplicate_runtime_error(
    tmp_path: Path,
) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        assert (
            client.post(f"/chat/{chat_id}/msg", json={"text": "hi"}).json()["status"]
            == "accepted"
        )
        _ingest_turn_started(client, chat_id)

    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        assert _state(client, chat_id) == "idle"

    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        assert _state(client, chat_id) == "idle"
        assert _event_types(client, chat_id, 3) == ["chat.started", "turn.started", "runtime.error"]

    assert _runtime_error_count(tmp_path, chat_id) == 1


def test_restart_recovery_writes_runtime_error_to_sqlite_index(tmp_path: Path) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        assert (
            client.post(f"/chat/{chat_id}/msg", json={"text": "hi"}).json()["status"]
            == "accepted"
        )
        _ingest_turn_started(client, chat_id)

    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        assert _state(client, chat_id) == "idle"

    conn = sqlite3.connect(_index_path(tmp_path, chat_id))
    row = conn.execute(
        "SELECT payload_json FROM events WHERE chat_id=? AND type='runtime.error'",
        (chat_id,),
    ).fetchone()
    conn.close()

    assert row == ('{"reason":"backend_lost_after_restart"}',)


def test_restart_recovery_keeps_closed_chat_persisted_only_and_replayable(
    tmp_path: Path,
) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        assert client.post(f"/chat/{chat_id}/close").json()["status"] == "accepted"

    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        assert _state(client, chat_id) == "closed"
        assert chat_id not in server._runtime.live_entries
        assert chat_id in server._runtime.persisted_only
        assert _event_types(client, chat_id, 2) == ["chat.started", "chat.exited"]
