"""Per-spawn control server."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from meridian.lib.core.types import SpawnId
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.streaming.types import InjectResult

if TYPE_CHECKING:
    from meridian.lib.streaming.spawn_manager import SpawnManager


class ControlSocketServer:
    """Handle one control socket endpoint for one active spawn."""

    def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: SpawnManager):
        self._spawn_id = spawn_id
        self._socket_path = socket_path
        self._port_file = socket_path.with_suffix(".port")
        self._manager = manager
        self._server: asyncio.AbstractServer | None = None
        self._port: int | None = None

    @property
    def discovery_path(self) -> Path:
        """Return the platform-specific discovery artifact path."""

        return self._port_file if IS_WINDOWS else self._socket_path

    @property
    def endpoint(self) -> str:
        """Return the platform-aware control endpoint for operator display."""

        if IS_WINDOWS:
            port = self._port
            if port is None:
                with suppress(OSError, ValueError):
                    port = int(self._port_file.read_text(encoding="utf-8").strip())
            if isinstance(port, int):
                return f"tcp://127.0.0.1:{port}"
            return f"tcp://127.0.0.1:<pending> (port file: {self._port_file})"
        return f"unix://{self._socket_path}"

    async def start(self) -> None:
        """Create and bind the per-spawn control endpoint."""

        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if IS_WINDOWS:
            self._port_file.unlink(missing_ok=True)
            self._server = await asyncio.start_server(
                self._handle_client,
                host="127.0.0.1",
                port=0,
            )
            sockets = self._server.sockets or ()
            if not sockets:
                raise RuntimeError("control socket server did not expose a bound port")
            addr = cast("object", sockets[0].getsockname())
            port_value: int | None = None
            if isinstance(addr, tuple):
                addr_tuple = cast("tuple[object, ...]", addr)
                if len(addr_tuple) >= 2 and isinstance(addr_tuple[1], int):
                    port_value = addr_tuple[1]
            if not isinstance(port_value, int):
                raise RuntimeError("control socket server returned invalid bound port")
            self._port = port_value
            self._port_file.write_text(f"{port_value}\n", encoding="utf-8")
            return

        self._socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Read one request, route to the manager, and write one response."""

        response: dict[str, object]
        try:
            raw = await reader.readline()
            if not raw:
                response = {"ok": False, "error": "empty request"}
            else:
                response = await self._handle_request(raw)
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}

        encoded = (
            json.dumps(response, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        writer.write(encoded)
        with suppress(BrokenPipeError, ConnectionResetError):
            await writer.drain()
        writer.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await writer.wait_closed()

    async def _handle_request(
        self,
        raw: bytes,
    ) -> dict[str, object]:
        """Decode and route one control request."""

        try:
            payload_value: object = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "error": "invalid JSON request"}

        if not isinstance(payload_value, dict):
            return {"ok": False, "error": "request must be a JSON object"}
        payload = cast("dict[str, object]", payload_value)

        message_type = payload.get("type")
        if not isinstance(message_type, str):
            return {"ok": False, "error": "missing request type"}

        result: InjectResult
        if message_type == "user_message":
            text = payload.get("text")
            if not isinstance(text, str):
                return {"ok": False, "error": "user_message requires text"}
            response: dict[str, object] | None = None

            def _on_result(inject_result: InjectResult) -> None:
                nonlocal response
                response = self._result_to_response(inject_result)

            result = await self._manager.inject(
                self._spawn_id,
                message=text,
                source="control_socket",
                on_result=_on_result,
            )
            return response or self._result_to_response(result)
        else:
            return {"ok": False, "error": f"unsupported request type: {message_type}"}

    @staticmethod
    def _result_to_response(result: InjectResult) -> dict[str, object]:
        response: dict[str, object]
        if result.success:
            response = {"ok": True}
        else:
            response = {"ok": False, "error": result.error or "request failed"}
        if result.inbound_seq is not None:
            response["inbound_seq"] = result.inbound_seq
        return response

    async def stop(self) -> None:
        """Close the server and remove its discovery artifact."""

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if IS_WINDOWS:
            self._port = None
            self._port_file.unlink(missing_ok=True)
            return

        self._socket_path.unlink(missing_ok=True)


__all__ = ["ControlSocketServer"]
