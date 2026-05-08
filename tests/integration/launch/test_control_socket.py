from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.streaming import control_socket as control_socket_module
from meridian.lib.streaming.control_socket import ControlSocketServer
from meridian.lib.streaming.types import InjectResult


class _FakeManager:
    def __init__(self, *, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.inject_calls: list[tuple[SpawnId, str, str]] = []
        self.interrupt_calls: list[tuple[SpawnId, str]] = []
        self.permission_replies: list[tuple[SpawnId, str, str, dict[str, object] | None, str]] = []
        self.user_input_replies: list[tuple[SpawnId, str, dict[str, object], str]] = []

    async def inject(
        self,
        spawn_id: SpawnId,
        message: str,
        *,
        source: str,
        on_result=None,
    ) -> InjectResult:
        self.inject_calls.append((spawn_id, message, source))
        result = InjectResult(success=True, inbound_seq=4)
        if on_result is not None:
            on_result(result)
        return result

    async def interrupt(self, spawn_id: SpawnId, *, source: str) -> None:
        self.interrupt_calls.append((spawn_id, source))

    async def respond_request(
        self,
        spawn_id: SpawnId,
        *,
        request_id: str,
        decision: str,
        payload: dict[str, object] | None = None,
        source: str,
    ) -> None:
        self.permission_replies.append((spawn_id, request_id, decision, payload, source))

    async def respond_user_input(
        self,
        spawn_id: SpawnId,
        *,
        request_id: str,
        answers: dict[str, object],
        source: str,
    ) -> None:
        self.user_input_replies.append((spawn_id, request_id, answers, source))


@pytest.mark.asyncio
async def test_user_message_request_requires_text(tmp_path: Path) -> None:
    manager = _FakeManager(runtime_root=tmp_path / ".meridian")
    server = ControlSocketServer(SpawnId("p1"), tmp_path / "control.sock", manager)

    result = await server._handle_request(b'{"type":"user_message"}\n')

    assert result == {"ok": False, "error": "user_message requires text"}
    assert manager.inject_calls == []


@pytest.mark.asyncio
async def test_control_socket_rejects_unsupported_message_types(tmp_path: Path) -> None:
    manager = _FakeManager(runtime_root=tmp_path / ".meridian")
    server = ControlSocketServer(SpawnId("p1"), tmp_path / "control.sock", manager)

    result = await server._handle_request(b'{"type":"unknown"}\n')

    assert result == {"ok": False, "error": "unsupported request type: unknown"}
    assert manager.inject_calls == []


@pytest.mark.asyncio
async def test_interrupt_routes_to_spawn_manager(tmp_path: Path) -> None:
    manager = _FakeManager(runtime_root=tmp_path / ".meridian")
    server = ControlSocketServer(SpawnId("p1"), tmp_path / "control.sock", manager)

    result = await server._handle_request(b'{"type":"interrupt"}\n')

    assert result == {"ok": True}
    assert manager.interrupt_calls == [(SpawnId("p1"), "control_socket")]


@pytest.mark.asyncio
async def test_permission_reply_routes_to_spawn_manager(tmp_path: Path) -> None:
    manager = _FakeManager(runtime_root=tmp_path / ".meridian")
    server = ControlSocketServer(SpawnId("p1"), tmp_path / "control.sock", manager)

    result = await server._handle_request(
        b'{"type":"permission_reply","request_id":"r1","decision":"accept","payload":{"x":1}}\n'
    )

    assert result == {"ok": True}
    assert manager.permission_replies == [
        (SpawnId("p1"), "r1", "accept", {"x": 1}, "control_socket")
    ]


@pytest.mark.asyncio
async def test_user_input_reply_routes_to_spawn_manager(tmp_path: Path) -> None:
    manager = _FakeManager(runtime_root=tmp_path / ".meridian")
    server = ControlSocketServer(SpawnId("p1"), tmp_path / "control.sock", manager)

    result = await server._handle_request(
        b'{"type":"user_input_reply","request_id":"u1","answers":{"text":"Ada"}}\n'
    )

    assert result == {"ok": True}
    assert manager.user_input_replies == [
        (SpawnId("p1"), "u1", {"text": "Ada"}, "control_socket")
    ]


def test_control_socket_endpoint_posix_discovery_path_and_display(tmp_path: Path) -> None:
    manager = _FakeManager(runtime_root=tmp_path / ".meridian")
    server = ControlSocketServer(SpawnId("p1"), tmp_path / "control.sock", manager)

    assert server.discovery_path == tmp_path / "control.sock"
    assert server.endpoint == f"unix://{tmp_path / 'control.sock'}"


def test_control_socket_endpoint_windows_reads_port_discovery_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control_socket_module, "IS_WINDOWS", True)
    manager = _FakeManager(runtime_root=tmp_path / ".meridian")
    server = ControlSocketServer(SpawnId("p1"), tmp_path / "control.sock", manager)
    port_file = tmp_path / "control.port"
    port_file.write_text("19090\n", encoding="utf-8")

    assert server.discovery_path == port_file
    assert server.endpoint == "tcp://127.0.0.1:19090"
