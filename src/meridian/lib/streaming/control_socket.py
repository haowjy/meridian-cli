"""Per-spawn control server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from meridian.lib.core.types import SpawnId
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.atomic import atomic_replace
from meridian.lib.streaming.types import InjectResult

if TYPE_CHECKING:
    from meridian.lib.streaming.spawn_manager import SpawnManager


# sockaddr_un.sun_path includes the trailing NUL. Use the pathname capacity so
# callers can validate before asyncio reaches the less actionable bind error.
UNIX_SOCKET_PATH_MAX_BYTES = 103 if sys.platform == "darwin" else 107


def control_socket_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    """Return the deterministic, bounded control endpoint for a spawn."""

    if IS_WINDOWS:
        return runtime_root / "spawns" / str(spawn_id) / "control.sock"

    uid = os.getuid()
    identity = os.fsencode(runtime_root.resolve()) + b"\0" + str(spawn_id).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:32]
    path = Path(tempfile.gettempdir()) / f"meridian-{uid}" / f"control-{digest}.sock"
    path_length = len(os.fsencode(path))
    if path_length > UNIX_SOCKET_PATH_MAX_BYTES:
        msg = (
            f"control socket path is {path_length} bytes, exceeding the platform "
            f"limit of {UNIX_SOCKET_PATH_MAX_BYTES}: {path}"
        )
        raise ValueError(msg)
    return path


def _prepare_socket_directory(path: Path) -> None:
    """Create a private per-user socket directory under the shared temp root."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_stat = path.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid():
        msg = f"control socket directory is not owned by the current user: {path}"
        raise PermissionError(msg)
    path.chmod(0o700)


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

        if IS_WINDOWS:
            self._socket_path.parent.mkdir(parents=True, exist_ok=True)
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
            with atomic_replace(self._port_file, durable=False) as handle:
                handle.write(f"{port_value}\n")
            return

        _prepare_socket_directory(self._socket_path.parent)
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
            json.dumps(response, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
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
        if message_type == "interrupt":
            await self._manager.interrupt(self._spawn_id, source="control_socket")
            return {"ok": True}
        if message_type == "permission_reply":
            request_id = payload.get("request_id")
            decision = payload.get("decision")
            reply_payload = payload.get("payload")
            if not isinstance(request_id, str) or not request_id:
                return {"ok": False, "error": "permission_reply requires request_id"}
            if not isinstance(decision, str) or not decision:
                return {"ok": False, "error": "permission_reply requires decision"}
            if reply_payload is not None and not isinstance(reply_payload, dict):
                return {"ok": False, "error": "permission_reply payload must be an object"}
            typed_payload = (
                cast("dict[str, object]", reply_payload)
                if isinstance(reply_payload, dict)
                else None
            )
            await self._manager.respond_request(
                self._spawn_id,
                request_id=request_id,
                decision=decision,
                payload=typed_payload,
                source="control_socket",
            )
            return {"ok": True}
        if message_type == "user_input_reply":
            request_id = payload.get("request_id")
            answers = payload.get("answers")
            if not isinstance(request_id, str) or not request_id:
                return {"ok": False, "error": "user_input_reply requires request_id"}
            if not isinstance(answers, dict):
                return {"ok": False, "error": "user_input_reply requires answers object"}
            typed_answers = cast("dict[str, object]", answers)
            await self._manager.respond_user_input(
                self._spawn_id,
                request_id=request_id,
                answers=typed_answers,
                source="control_socket",
            )
            return {"ok": True}
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


__all__ = [
    "UNIX_SOCKET_PATH_MAX_BYTES",
    "ControlSocketServer",
    "control_socket_path",
]
