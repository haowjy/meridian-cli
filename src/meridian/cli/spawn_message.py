"""CLI helper for sending user messages into active streaming spawns."""

from __future__ import annotations

import asyncio
import errno
import json
import sys
import time
from pathlib import Path
from typing import Never, cast

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.harness.bundle import (
    PrimaryInboxError,
    post_to_primary,
    primary_accepts_messages,
)
from meridian.lib.ops.runtime import (
    resolve_runtime_root,
    resolve_runtime_root_and_config,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import read_derived
from meridian.lib.streaming.control_socket import control_socket_path

_RESPONSE_TIMEOUT_SECONDS = 30.0
_ENDPOINT_DISCOVERY_TIMEOUT_SECONDS = 30.0
_ENDPOINT_DISCOVERY_POLL_SECONDS = 0.1


def _fail(message: str) -> Never:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def framed_message(
    message: str,
    *,
    sender_spawn_id: str | None = None,
    sender_chat_id: str | None = None,
    can_reply: bool = False,
) -> str:
    """Open the message with its sender and, when it exists, a way to answer.

    A session that receives a message otherwise cannot tell which session sent
    it, or where to reply. The frame is text, not wire metadata: every harness's
    delivery path carries it unchanged.
    """

    if sender_spawn_id is None:
        provenance = "[Meridian] Message with no sender identity; it has no reply address."
    else:
        where = f"spawn {sender_spawn_id}"
        if sender_chat_id:
            where += f" (chat {sender_chat_id})"
        if can_reply:
            provenance = (
                f"[Meridian] Message from {where}. To answer, run "
                f'`meridian spawn message {sender_spawn_id} --message "..."`.'
            )
        else:
            provenance = f"[Meridian] Message from {where}; it has no reply route here."
    return f"{provenance}\n\n{message}"


def _sender_record(runtime_root: Path, sender_spawn_id: str | None) -> SpawnRecord | None:
    """Return the sender's own record, the source of its chat id and route."""

    if sender_spawn_id is None:
        return None
    return read_derived(runtime_root, sender_spawn_id)


def _can_receive_reply(sender: SpawnRecord | None) -> bool:
    """True when a message sent back to the sender would reach a live route."""

    if sender is None or not is_active_spawn_status(sender.status):
        return False
    return sender.kind != "primary" or primary_accepts_messages(sender)


async def _send_and_receive(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    request: dict[str, str],
) -> bytes:
    """Send request and receive response, with proper cleanup."""
    try:
        writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()
        return await asyncio.wait_for(reader.readline(), timeout=_RESPONSE_TIMEOUT_SECONDS)
    finally:
        writer.close()
        await writer.wait_closed()


def _post_primary(runtime_root: Path, record: SpawnRecord, message: str) -> None:
    if not is_active_spawn_status(record.status):
        _fail(f"spawn not running: {record.id}")
    try:
        confirmation = post_to_primary(record, runtime_root=runtime_root, message=message)
    except PrimaryInboxError as exc:
        _fail(str(exc))
    print(confirmation)


async def send_message(
    spawn_id: str,
    message: str,
    *,
    sender_spawn_id: str | None = None,
) -> None:
    """Post a user message to a running spawn: its control endpoint or a primary inbox."""

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

    sender = _sender_record(runtime_root, sender_spawn_id)
    framed = framed_message(
        normalized_message,
        sender_spawn_id=sender_spawn_id,
        sender_chat_id=sender.chat_id if sender is not None else None,
        can_reply=_can_receive_reply(sender),
    )

    record = read_derived(runtime_root, normalized_spawn_id)
    if record is not None and record.kind == "primary":
        _post_primary(runtime_root, record, framed)
        return

    discovery_deadline = time.monotonic() + _ENDPOINT_DISCOVERY_TIMEOUT_SECONDS
    while True:
        record = read_derived(runtime_root, normalized_spawn_id)
        if record is None or not is_active_spawn_status(record.status):
            _fail(f"spawn not running: {normalized_spawn_id} has no control endpoint")
        if socket_path.exists():
            break
        remaining = discovery_deadline - time.monotonic()
        if remaining <= 0:
            _fail(f"spawn not running: {normalized_spawn_id} has no control endpoint")
        await asyncio.sleep(min(_ENDPOINT_DISCOVERY_POLL_SECONDS, remaining))

    request: dict[str, str] = {"type": "user_message", "text": framed}

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
