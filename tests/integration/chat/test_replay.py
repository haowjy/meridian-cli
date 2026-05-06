from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

import meridian.lib.chat.server as server
from meridian.lib.chat.event_log import ChatEventLog
from meridian.lib.chat.protocol import ChatEvent, utc_now_iso
from meridian.lib.chat.replay import ReplayService
from meridian.lib.chat.server import app, configure
from meridian.lib.chat.ws_fanout import WebSocketFanOut
from meridian.lib.core.types import SpawnId


class Handle:
    spawn_id = SpawnId("p-test")

    def health(self) -> bool:
        return True

    async def send_message(self, text: str) -> None:
        pass

    async def send_cancel(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def respond_request(
        self,
        request_id: str,
        decision: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        _ = (request_id, decision, payload)

    async def respond_user_input(self, request_id: str, answers: dict[str, object]) -> None:
        _ = (request_id, answers)


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


class RecordingWebSocket:
    def __init__(self, *, send_delay: float = 0.0) -> None:
        self.sent: list[dict[str, object]] = []
        self.closed: list[tuple[int, str | None]] = []
        self._send_delay = send_delay
        self._sent_event = asyncio.Event()

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)
        self._sent_event.set()
        if self._send_delay:
            await asyncio.sleep(self._send_delay)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append((code, reason))

    async def wait_for_count(self, count: int, *, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while len(self.sent) < count:
                self._sent_event.clear()
                await self._sent_event.wait()



def _event(chat_id: str, seq: int, event_type: str, *, execution_id: str = "p-test") -> ChatEvent:
    return ChatEvent(
        type=event_type,
        seq=seq,
        chat_id=chat_id,
        execution_id=execution_id,
        timestamp=utc_now_iso(),
    )


async def _broadcast_during_replay(
    fanout: WebSocketFanOut,
    event: ChatEvent,
    websocket: RecordingWebSocket,
    *,
    after_count: int,
) -> None:
    await websocket.wait_for_count(after_count)
    await fanout.broadcast(event)



def test_replay_without_last_seq_and_with_last_seq(tmp_path: Path) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        client.post(f"/chat/{chat_id}/close")

        with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
            first = ws.receive_json()
            second = ws.receive_json()
        assert first["type"] == "chat.started"
        assert second["type"] == "chat.exited"

        with client.websocket_connect(f"/ws/chat/{chat_id}?last_seq={first['seq']}") as ws:
            assert ws.receive_json()["type"] == "chat.exited"



def test_replay_deduplicates_replay_to_live_boundary_when_last_seq_is_present(
    tmp_path: Path,
) -> None:
    chat_id = "c-replay"
    event_log = ChatEventLog(tmp_path / "history.jsonl")
    fanout = WebSocketFanOut()
    websocket = RecordingWebSocket(send_delay=0.01)
    replay = ReplayService(event_log, fanout)

    persisted = [_event(chat_id, 0, "chat.started")]
    persisted.extend(_event(chat_id, index, "content.delta") for index in range(1, 6))
    for event in persisted:
        event_log.append(event)
    duplicate_boundary_event = persisted[-1]

    async def exercise() -> list[dict[str, object]]:
        connect_task = asyncio.create_task(replay.connect(websocket, last_seq=1))
        duplicate_task = asyncio.create_task(
            _broadcast_during_replay(
                fanout,
                duplicate_boundary_event,
                websocket,
                after_count=2,
            )
        )
        await asyncio.wait_for(duplicate_task, timeout=5)
        await websocket.wait_for_count(4)
        fanout.unregister(websocket)
        await asyncio.wait_for(connect_task, timeout=5)
        return list(websocket.sent)

    sent = asyncio.run(exercise())

    assert [payload["seq"] for payload in sent] == [2, 3, 4, 5]
    assert len([payload for payload in sent if payload["seq"] == 5]) == 1



def test_two_clients_receive_same_live_stream_and_disconnecting_one_does_not_affect_the_other(
    tmp_path: Path,
) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]

        with client.websocket_connect(f"/ws/chat/{chat_id}") as first:
            with client.websocket_connect(f"/ws/chat/{chat_id}") as second:
                assert first.receive_json()["type"] == "chat.started"
                assert second.receive_json()["type"] == "chat.started"

                assert (
                    client.post(f"/chat/{chat_id}/msg", json={"text": "hi"}).json()["status"]
                    == "accepted"
                )
                assert (
                    client.post(
                        f"/chat/{chat_id}/approve",
                        json={"request_id": "r1", "decision": "accept", "payload": {"x": 1}},
                    ).json()["status"]
                    == "accepted"
                )
                assert (
                    client.post(
                        f"/chat/{chat_id}/input",
                        json={"request_id": "i1", "answers": {"name": "Ada"}},
                    ).json()["status"]
                    == "accepted"
                )
                client.portal.call(server._runtime.live_entries[chat_id].pipeline.drain)

                persisted_events = server._runtime.list_events(chat_id)
                assert persisted_events is not None
                remaining_event_count = len(persisted_events) - 1
                first_events = [first.receive_json() for _ in range(remaining_event_count)]
                second_events = [second.receive_json() for _ in range(remaining_event_count)]
                assert first_events == second_events

            assert client.post(f"/chat/{chat_id}/close").json()["status"] == "accepted"
            trailing = first.receive_json()

    relevant_events = [
        event for event in first_events if event["type"] in {"request.resolved", "user_input.resolved"}
    ]
    assert [event["type"] for event in relevant_events] == [
        "request.resolved",
        "user_input.resolved",
    ]
    assert trailing["type"] == "chat.exited"
