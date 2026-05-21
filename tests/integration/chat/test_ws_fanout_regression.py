from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from meridian.lib.chat.event_log import ChatEventLog
from meridian.lib.chat.policy import default_chat_policy_snapshot
from meridian.lib.chat.protocol import CHAT_EXITED, CHAT_STARTED, ChatEvent, utc_now_iso
from meridian.lib.chat.replay import ReplayService
from meridian.lib.chat.runtime import ChatRuntime
from meridian.lib.chat.server import app, configure
from meridian.lib.chat.ws_fanout import WebSocketFanOut
from meridian.lib.core.types import SpawnId


class PassiveHandle:
    spawn_id = SpawnId("p-test")

    def health(self) -> bool:
        return True

    async def send_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class PassiveAcquisition:
    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ) -> Any:
        _ = (chat_id, initial_prompt, execution_generation, chat_state)
        return cast("Any", PassiveHandle())


class RecordingWebSocket:
    def __init__(self, *, send_delay: float = 0.0) -> None:
        self.sent: list[dict[str, object]] = []
        self.closed: list[tuple[int, str | None]] = []
        self._send_delay = send_delay
        self._sent_event = asyncio.Event()
        self._closed_event = asyncio.Event()

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)
        self._sent_event.set()
        if self._send_delay:
            await asyncio.sleep(self._send_delay)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append((code, reason))
        self._closed_event.set()

    async def wait_for_count(self, count: int, *, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while len(self.sent) < count:
                self._sent_event.clear()
                await self._sent_event.wait()

    async def wait_for_close(self, *, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            if not self.closed:
                await self._closed_event.wait()


def _event(chat_id: str, seq: int, event_type: str = "content.delta") -> ChatEvent:
    return ChatEvent(
        type=event_type,
        seq=seq,
        chat_id=chat_id,
        execution_id="p-test",
        timestamp=utc_now_iso(),
    )


def _receive_until(
    websocket: Any,
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    max_messages: int,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for _ in range(max_messages):
        payload = websocket.receive_json()
        messages.append(payload)
        if predicate(messages):
            return messages
    raise AssertionError(f"condition not met after {max_messages} messages: {messages!r}")


def _has_ack_and_event(
    messages: list[dict[str, Any]],
    *,
    command_id: str,
    event_type: str,
) -> bool:
    return any(payload.get("ack") == command_id for payload in messages) and any(
        payload.get("type") == event_type for payload in messages
    )


def _payload_seq(payload: dict[str, object]) -> int:
    raw = payload["seq"]
    assert isinstance(raw, int)
    return raw


def _wait_for(predicate: Any, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def test_reconnect_with_last_seq_replays_missed_events_over_websocket_transport(
    tmp_path: Path,
) -> None:
    runtime = ChatRuntime(
        runtime_root=tmp_path,
        project_root=tmp_path,
        default_policy_snapshot=default_chat_policy_snapshot(),
        backend_acquisition=cast("Any", PassiveAcquisition()),
    )
    configure(runtime=runtime)

    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]

        with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
            started = ws.receive_json()

        assert started["type"] == CHAT_STARTED
        assert started["seq"] == 0

        closed = client.post(f"/chat/{chat_id}/close").json()
        assert closed == {"status": "accepted", "error": None}

        with client.websocket_connect(f"/ws/chat/{chat_id}?last_seq={started['seq']}") as ws:
            replayed = ws.receive_json()

    assert replayed["type"] == CHAT_EXITED
    assert replayed["seq"] == 1


def test_backpressure_eviction_drops_slow_client_without_affecting_fast_clients(
    tmp_path: Path,
) -> None:
    chat_id = "c-backpressure-fast"
    event_log = ChatEventLog(tmp_path / "history.jsonl")
    fanout = WebSocketFanOut(max_buffer=2)
    slow_websocket = RecordingWebSocket(send_delay=0.05)
    fast_websocket = RecordingWebSocket()
    replay = ReplayService(event_log, fanout)

    async def exercise() -> tuple[
        list[dict[str, object]],
        list[tuple[int, str | None]],
        list[dict[str, object]],
    ]:
        event_log.append(_event(chat_id, 0, CHAT_STARTED))
        slow_task = asyncio.create_task(replay.connect(cast("Any", slow_websocket)))
        fast_task = asyncio.create_task(replay.connect(cast("Any", fast_websocket)))
        await slow_websocket.wait_for_count(1)
        await fast_websocket.wait_for_count(1)

        for seq in range(1, 7):
            event = _event(chat_id, seq)
            event_log.append(event)
            await fanout.broadcast(event)
            await asyncio.sleep(0)

        await slow_websocket.wait_for_close()
        await fast_websocket.wait_for_count(7)

        slow_task.cancel()
        fast_task.cancel()
        with suppress(asyncio.CancelledError):
            await slow_task
        with suppress(asyncio.CancelledError):
            await fast_task
        return slow_websocket.sent, slow_websocket.closed, fast_websocket.sent

    slow_payloads, slow_closed, fast_payloads = asyncio.run(exercise())

    slow_seqs = [_payload_seq(payload) for payload in slow_payloads]
    fast_seqs = [_payload_seq(payload) for payload in fast_payloads]

    assert slow_closed == [(1008, "backpressure:reconnect_with_last_seq")]
    assert slow_seqs == list(range(slow_seqs[-1] + 1))
    assert len(slow_seqs) < 7
    assert fast_seqs == list(range(7))


def test_websocket_command_ack_framing_matches_each_command_id(tmp_path: Path) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=cast("Any", PassiveAcquisition()))

    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
            assert ws.receive_json()["type"] == CHAT_STARTED

            ws.send_json(
                {
                    "command_type": "prompt",
                    "command_id": "cmd-prompt",
                    "chat_id": chat_id,
                    "timestamp": "2026-04-30T00:00:00Z",
                    "payload": {"text": "hello"},
                }
            )
            prompt_messages = _receive_until(
                ws,
                lambda messages: any(payload.get("ack") == "cmd-prompt" for payload in messages),
                max_messages=4,
            )
            assert {payload["ack"] for payload in prompt_messages if "ack" in payload} == {
                "cmd-prompt"
            }

            ws.send_json(
                {
                    "command_type": "cancel",
                    "command_id": "cmd-cancel",
                    "chat_id": chat_id,
                    "timestamp": "2026-04-30T00:00:01Z",
                    "payload": {},
                }
            )
            cancel_messages = _receive_until(
                ws,
                lambda messages: any(payload.get("ack") == "cmd-cancel" for payload in messages),
                max_messages=4,
            )
            assert {payload["ack"] for payload in cancel_messages if "ack" in payload} == {
                "cmd-cancel"
            }

            ws.send_json(
                {
                    "command_type": "close",
                    "command_id": "cmd-close",
                    "chat_id": chat_id,
                    "timestamp": "2026-04-30T00:00:02Z",
                    "payload": {},
                }
            )
            close_messages = _receive_until(
                ws,
                lambda messages: _has_ack_and_event(
                    messages, command_id="cmd-close", event_type=CHAT_EXITED
                ),
                max_messages=2,
            )

    assert {payload["ack"] for payload in close_messages if "ack" in payload} == {"cmd-close"}
    assert {payload["type"] for payload in close_messages if "type" in payload} == {CHAT_EXITED}


def test_malformed_websocket_message_rejects_sender_without_crashing_other_clients(
    tmp_path: Path,
) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=cast("Any", PassiveAcquisition()))

    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        with (
            client.websocket_connect(f"/ws/chat/{chat_id}") as bad_ws,
            client.websocket_connect(f"/ws/chat/{chat_id}") as good_ws,
        ):
            assert bad_ws.receive_json()["type"] == CHAT_STARTED
            assert good_ws.receive_json()["type"] == CHAT_STARTED

            bad_ws.send_json(
                {
                    "command_type": "prompt",
                    "command_id": "cmd-bad",
                    "chat_id": chat_id,
                    "timestamp": "2026-04-30T00:00:00Z",
                    "payload": "not-an-object",
                }
            )
            assert bad_ws.receive_json() == {
                "ack": "cmd-bad",
                "status": "rejected",
                "error": "invalid_command:payload_not_object",
            }

            good_ws.send_json(
                {
                    "command_type": "close",
                    "command_id": "cmd-good-close",
                    "chat_id": chat_id,
                    "timestamp": "2026-04-30T00:00:01Z",
                    "payload": {},
                }
            )
            good_messages = _receive_until(
                good_ws,
                lambda messages: _has_ack_and_event(
                    messages, command_id="cmd-good-close", event_type=CHAT_EXITED
                ),
                max_messages=2,
            )
            bad_close = bad_ws.receive_json()

    assert {payload["ack"] for payload in good_messages if "ack" in payload} == {"cmd-good-close"}
    assert {payload["type"] for payload in good_messages if "type" in payload} == {CHAT_EXITED}
    assert bad_close["type"] == CHAT_EXITED


