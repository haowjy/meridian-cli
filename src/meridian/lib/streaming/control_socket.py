"""Per-spawn control server."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import socket
import stat
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal, cast

from meridian.lib.core.delivery import DeliveryError, MessageRoute
from meridian.lib.core.types import SpawnId
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.atomic import atomic_replace

# sockaddr_un.sun_path includes the trailing NUL. Use the pathname capacity so
# callers can validate before asyncio reaches the less actionable bind error.
UNIX_SOCKET_PATH_MAX_BYTES = 103 if sys.platform == "darwin" else 107
# Runner and client processes may have different TMPDIR values; the shared
# discovery path must not depend on either process's environment.
_POSIX_SOCKET_ROOT = Path("/tmp")
# One request is one line; asyncio's 64 KiB default would cap messages far below what
# the harnesses accept.
MAX_REQUEST_BYTES = 16 * 1024 * 1024
# A client that connects and stays silent must not pin a handler, or hang stop().
_REQUEST_READ_TIMEOUT_SECONDS = 10.0
_STOP_WAIT_SECONDS = 5.0
# bind() creates the file before the kernel publishes the bound socket.
_STALE_SOCKET_AGE_SECONDS = 120


def socket_directory() -> Path:
    """Return the shared per-user directory for POSIX control sockets."""

    return _POSIX_SOCKET_ROOT / f"meridian-{os.getuid()}"


def bounded_socket_path(
    runtime_root: Path,
    kind: Literal["app", "control"],
    key: str | None = None,
) -> Path:
    """Return a deterministic Unix socket path below the platform limit."""

    identity = os.fsencode(runtime_root.resolve())
    if key is not None:
        identity += b"\0" + key.encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:32]
    path = socket_directory() / f"{kind}-{digest}.sock"
    path_length = len(os.fsencode(path))
    if path_length > UNIX_SOCKET_PATH_MAX_BYTES:
        msg = (
            f"{kind} socket path is {path_length} bytes, exceeding the platform "
            f"limit of {UNIX_SOCKET_PATH_MAX_BYTES}: {path}"
        )
        raise ValueError(msg)
    return path


def control_socket_path(runtime_root: Path, spawn_id: SpawnId | str) -> Path:
    """Return the deterministic, bounded control endpoint for a spawn."""

    if IS_WINDOWS:
        return runtime_root / "spawns" / str(spawn_id) / "control.sock"

    return bounded_socket_path(runtime_root, "control", str(spawn_id))


def prepare_socket_directory(path: Path) -> None:
    """Create a private per-user socket directory under the fixed POSIX root."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_stat = path.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid():
        msg = f"socket directory is not owned by the current user: {path}"
        raise PermissionError(msg)
    path.chmod(0o700)


def _sweep_stale_sockets(own_path: Path) -> None:
    """Recover abandoned endpoints without connecting to a live stream server."""

    cutoff = time.time() - _STALE_SOCKET_AGE_SECONDS
    uid = os.getuid()
    with os.scandir(own_path.parent) as entries:
        for entry in entries:
            if entry.name == own_path.name or not re.fullmatch(
                r"control-[0-9a-f]{32}\.sock", entry.name
            ):
                continue
            # Other runners may remove entries while we scan; inaccessible entries
            # are not evidence of an abandoned endpoint either.
            with suppress(OSError):
                before = entry.stat(follow_symlinks=False)
                if (
                    not stat.S_ISSOCK(before.st_mode)
                    or before.st_uid != uid
                    or before.st_mtime > cutoff
                ):
                    continue
                # A datagram probe gets EPROTOTYPE for a bound stream socket, even
                # before listen() or with a full backlog. A stream probe can get
                # ECONNREFUSED for a live, backlog-full listener on macOS.
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as probe:
                    probe.setblocking(False)
                    if probe.connect_ex(entry.path) != errno.ECONNREFUSED:
                        continue
                # Preserve external replacements; the launch lock serializes runners of one spawn.
                after = os.lstat(entry.path)
                if (before.st_dev, before.st_ino, before.st_ctime_ns) == (
                    after.st_dev,
                    after.st_ino,
                    after.st_ctime_ns,
                ):
                    os.unlink(entry.path)


class ControlSocketServer:
    """Serve one control socket endpoint: each request is one message for the route."""

    def __init__(self, socket_path: Path, route: MessageRoute):
        self._socket_path = socket_path
        self._port_file = socket_path.with_suffix(".port")
        self._route = route
        self._server: asyncio.AbstractServer | None = None
        self._port: int | None = None

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
                limit=MAX_REQUEST_BYTES,
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

        prepare_socket_directory(self._socket_path.parent)
        _sweep_stale_sockets(self._socket_path)
        # The runner holds this spawn's launch.lock for the endpoint lifetime.
        self._socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path), limit=MAX_REQUEST_BYTES
        )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Read one request, route to the manager, and write one response."""

        response: dict[str, object]
        try:
            raw = await asyncio.wait_for(reader.readline(), _REQUEST_READ_TIMEOUT_SECONDS)
        except TimeoutError:
            response = {"ok": False, "error": "no request received"}
        except ValueError:
            response = {
                "ok": False,
                "error": f"message is over the {MAX_REQUEST_BYTES // (1024 * 1024)} MiB limit",
            }
        else:
            try:
                if raw:
                    response = await self._handle_request(raw)
                else:
                    response = {"ok": False, "error": "empty request"}
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

        if message_type != "user_message":
            return {"ok": False, "error": f"unsupported request type: {message_type}"}
        text = payload.get("text")
        if not isinstance(text, str):
            return {"ok": False, "error": "user_message requires text"}
        try:
            delivery = await self._route.deliver(text)
        except DeliveryError as exc:
            return {"ok": False, "error": str(exc)}
        response: dict[str, object] = {"ok": True, "delivery": delivery.value}
        if self._route.note is not None:
            response["note"] = self._route.note
        return response

    async def stop(self) -> None:
        """Close the server and remove its discovery artifact."""

        server, self._server = self._server, None
        if server is not None:
            server.close()
        # Unlink first: a client that keeps its connection open can delay wait_closed.
        if IS_WINDOWS:
            self._port = None
            self._port_file.unlink(missing_ok=True)
        else:
            self._socket_path.unlink(missing_ok=True)
        if server is not None:
            with suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), _STOP_WAIT_SECONDS)


__all__ = [
    "UNIX_SOCKET_PATH_MAX_BYTES",
    "ControlSocketServer",
    "bounded_socket_path",
    "control_socket_path",
    "prepare_socket_directory",
    "socket_directory",
]
