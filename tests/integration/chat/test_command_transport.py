from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from meridian.lib.chat.server import app, configure
from meridian.lib.core.types import SpawnId


class Handle:
    spawn_id = SpawnId("p-test")

    def __init__(self) -> None:
        self.messages: list[str] = []

    def health(self) -> bool:
        return True

    async def send_message(self, text: str) -> None:
        self.messages.append(text)

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


class BlockingAcquisition:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, str, int]] = []
        self.handle = Handle()

    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ) -> Handle:
        _ = chat_state
        self.calls.append((chat_id, initial_prompt, execution_generation))
        self.started.set()
        self.release.wait(timeout=5)
        return self.handle


def _receive_ack(ws: Any, command_id: str, *, max_messages: int = 8) -> dict[str, Any]:
    for _ in range(max_messages):
        payload = ws.receive_json()
        if payload.get("ack") == command_id:
            return payload
    raise AssertionError(f"Did not receive ack for {command_id!r} within {max_messages} messages")


def test_websocket_command_ack_uses_command_type_discriminator(tmp_path: Path) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
            assert ws.receive_json()["type"] == "chat.started"
            ws.send_json({
                "command_type": "prompt",
                "command_id": "cmd-1",
                "chat_id": chat_id,
                "timestamp": "2026-04-29T00:00:00Z",
                "payload": {"text": "hi"},
            })
            assert _receive_ack(ws, "cmd-1") == {"ack": "cmd-1", "status": "accepted"}
            ws.send_json({
                "command_type": "prompt",
                "command_id": "cmd-2",
                "chat_id": chat_id,
                "timestamp": "2026-04-29T00:00:00Z",
                "payload": {"text": "again"},
            })
            assert _receive_ack(ws, "cmd-2") == {
                "ack": "cmd-2",
                "status": "rejected",
                "error": "concurrent_prompt",
            }


def test_multiple_websocket_clients_serialize_prompt_dispatch_through_shared_session(
    tmp_path: Path,
) -> None:
    acquisition = BlockingAcquisition()
    configure(runtime_root=tmp_path, backend_acquisition=acquisition)

    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        with (
            client.websocket_connect(f"/ws/chat/{chat_id}") as ws_one,
            client.websocket_connect(f"/ws/chat/{chat_id}") as ws_two,
        ):
            assert ws_one.receive_json()["type"] == "chat.started"
            assert ws_two.receive_json()["type"] == "chat.started"

            results: dict[str, dict[str, Any]] = {}

            def send_prompt(name: str, ws: Any, text: str) -> None:
                ws.send_json(
                    {
                        "command_type": "prompt",
                        "command_id": name,
                        "chat_id": chat_id,
                        "timestamp": "2026-04-30T00:00:00Z",
                        "payload": {"text": text},
                    }
                )
                results[name] = _receive_ack(ws, name)

            first = threading.Thread(target=send_prompt, args=("cmd-1", ws_one, "first"))
            second = threading.Thread(target=send_prompt, args=("cmd-2", ws_two, "second"))

            first.start()
            assert acquisition.started.wait(timeout=5)
            second.start()
            second.join(timeout=5)
            acquisition.release.set()
            first.join(timeout=5)
            second.join(timeout=5)
            assert not first.is_alive()
            assert not second.is_alive()

    assert acquisition.calls == [(chat_id, "first", 1)]
    assert sorted(results.values(), key=lambda item: item["ack"]) == [
        {"ack": "cmd-1", "status": "accepted"},
        {"ack": "cmd-2", "status": "rejected", "error": "concurrent_prompt"},
    ]


def test_websocket_transport_forwards_unknown_command_types_to_handler(tmp_path: Path) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
            assert ws.receive_json()["type"] == "chat.started"
            ws.send_json(
                {
                    "command_type": "future.command",
                    "command_id": "cmd-future",
                    "chat_id": chat_id,
                    "timestamp": "2026-04-30T00:00:00Z",
                    "payload": {},
                }
            )
            assert _receive_ack(ws, "cmd-future") == {
                "ack": "cmd-future",
                "status": "rejected",
                "error": "unknown_command_type:future.command",
            }


def test_malformed_websocket_command_is_rejected_with_correlated_ack(tmp_path: Path) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
            assert ws.receive_json()["type"] == "chat.started"
            ws.send_json(
                {
                    "command_type": "prompt",
                    "command_id": "cmd-bad",
                    "chat_id": chat_id,
                    "timestamp": "2026-04-30T00:00:00Z",
                    "payload": "not-an-object",
                }
            )
            payload = _receive_ack(ws, "cmd-bad")

    assert payload["ack"] == "cmd-bad"
    assert payload["status"] == "rejected"
    assert payload["error"] == "invalid_command:payload_not_object"
