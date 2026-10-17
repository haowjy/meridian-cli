"""CLI helper for injecting control messages into active streaming spawns."""

from __future__ import annotations

import asyncio
import errno
import json
import sys
import time
from typing import Never, cast

from meridian.lib.core.spawn_lifecycle import ACTIVE_SPAWN_STATUSES
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.ops.runtime import (
    resolve_runtime_root,
    resolve_runtime_root_and_config,
)
from meridian.lib.state import spawn_store
from meridian.lib.streaming.control_socket import control_socket_path

_INJECT_RESPONSE_TIMEOUT_SECONDS = 30.0
_ENDPOINT_DISCOVERY_TIMEOUT_SECONDS = 30.0
_ENDPOINT_DISCOVERY_POLL_SECONDS = 0.1


def _fail(message: str) -> Never:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


async def _send_and_receive(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    request: dict[str, str],
) -> bytes:
    """Send request and receive response, with proper cleanup."""
    try:
        writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()
        return await asyncio.wait_for(
            reader.readline(), timeout=_INJECT_RESPONSE_TIMEOUT_SECONDS
        )
    finally:
        writer.close()
        await writer.wait_closed()


async def inject_message(
    spawn_id: str,
    message: str,
) -> None:
    """Send a control message to a running bidirectional spawn over a Unix socket."""

    normalized_spawn_id = spawn_id.strip()
    if not normalized_spawn_id:
        _fail("spawn id is required")

    project_root, _ = resolve_runtime_root_and_config(None)
    runtime_root = resolve_runtime_root(project_root)
    spawn_dir = runtime_root / "spawns" / normalized_spawn_id
    socket_path = control_socket_path(runtime_root, normalized_spawn_id)

    if not spawn_dir.exists():
        _fail(f"spawn not found: {normalized_spawn_id}")

    normalized_message = message.strip()
    if not normalized_message:
        _fail("message is required")

    record = spawn_store.get_spawn(runtime_root, normalized_spawn_id)
    if record is not None and record.kind == "primary":
        if record.status not in ACTIVE_SPAWN_STATUSES:
            _fail(f"spawn not running: {normalized_spawn_id} has no control endpoint")

        harness_name = record.harness or "unknown"
        try:
            harness_id = HarnessId(harness_name)
        except ValueError:
            _fail(f"{harness_name} primaries have no inject route yet (see #498)")

        # Keep harness bootstrap and its adapter imports off non-primary inject paths.
        from meridian.lib.harness import ensure_bootstrap
        from meridian.lib.harness.bundle import PrimaryInboxError, get_harness_bundle

        ensure_bootstrap()
        primary_inbox = get_harness_bundle(harness_id).primary_inbox
        if primary_inbox is None:
            _fail(f"{harness_name} primaries have no inject route yet (see #498)")
        try:
            receipt = primary_inbox(
                runtime_root=runtime_root,
                spawn_id=SpawnId(normalized_spawn_id),
                worker_pid=record.worker_pid,
                message=normalized_message,
            )
        except PrimaryInboxError as exc:
            _fail(str(exc))
        print(receipt.confirmation)
        if receipt.advisory is not None:
            print(receipt.advisory)
        return

    discovery_deadline = time.monotonic() + _ENDPOINT_DISCOVERY_TIMEOUT_SECONDS
    while True:
        record = spawn_store.get_spawn(runtime_root, normalized_spawn_id)
        if record is None or record.status not in ACTIVE_SPAWN_STATUSES:
            _fail(f"spawn not running: {normalized_spawn_id} has no control endpoint")
        if socket_path.exists():
            break
        remaining = discovery_deadline - time.monotonic()
        if remaining <= 0:
            _fail(f"spawn not running: {normalized_spawn_id} has no control endpoint")
        await asyncio.sleep(min(_ENDPOINT_DISCOVERY_POLL_SECONDS, remaining))

    request: dict[str, str] = {"type": "user_message", "text": normalized_message}

    response_data = b""
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        response_data = await _send_and_receive(reader, writer, request)
    except TimeoutError:
        _fail(f"timed out waiting for response from spawn {normalized_spawn_id}")
    except ConnectionRefusedError:
        _fail(f"connection refused: spawn {normalized_spawn_id} is not running")
    except FileNotFoundError:
        _fail(f"spawn not running: {normalized_spawn_id} control socket disappeared")
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            _fail(f"spawn not running: {normalized_spawn_id} control socket disappeared")
        if exc.errno == errno.ECONNREFUSED:
            _fail(f"connection refused: spawn {normalized_spawn_id} is not running")
        raise

    if not response_data:
        _fail(f"spawn not running: {normalized_spawn_id} returned no response")

    parsed_obj: object | None = None
    try:
        parsed_obj = json.loads(response_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(f"invalid response from spawn {normalized_spawn_id}")
    if not isinstance(parsed_obj, dict):
        _fail(f"invalid response from spawn {normalized_spawn_id}")
    parsed = cast("dict[str, object]", parsed_obj)

    ok = parsed.get("ok")
    if ok is True:
        print(f"Message delivered to spawn {normalized_spawn_id}")
        return

    error_value = parsed.get("error")
    if isinstance(error_value, str) and error_value.strip():
        _fail(error_value.strip())
    _fail("unknown error")
