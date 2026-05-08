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
