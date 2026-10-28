"""CLI helper for sending messages into running spawns and live peer sessions."""

from __future__ import annotations

import asyncio
import errno
import json
import shlex
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
from meridian.lib.ops.spawn.query import resolve_message_target
from meridian.lib.state.session_identity import spawn_owner_chat_id
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
    sender: SpawnRecord | None = None,
    sender_project_root: Path | None = None,
    can_reply: bool = False,
) -> str:
    """Open the message with its sender and, when it exists, a way to answer.

    A session that receives a message otherwise cannot tell which session sent
    it, or where to reply. The frame is text, not wire metadata: every harness's
    delivery path carries it unchanged. *sender_project_root* is set only when the
    sender lives in another project than the receiver, which is when the reply
    needs ``-C``.
    """

    if sender is None:
        provenance = "[Meridian] Message with no sender identity; it has no reply address."
    else:
        where = f"spawn {sender.id}"
        if sender.chat_id:
            where += f" (chat {sender.chat_id})"
        if can_reply:
            # A chat ID names a primary across generations; a subagent is only its
            # spawn ID, since its chat resolves to the primary that launched it.
            address = sender.id
            if sender.kind == "primary":
                address = spawn_owner_chat_id(sender) or sender.id
            via = ""
            if sender_project_root is not None:
                via = f"-C {shlex.quote(sender_project_root.as_posix())} "
            provenance = (
                f"[Meridian] Message from {where}. To answer, run "
                f'`meridian {via}spawn message {address} --message "..."`.'
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
    ref: str,
    message: str,
    *,
    sender_spawn_id: str | None = None,
    sender_project_root: Path | None = None,
) -> None:
    """Post a user message to a running spawn or a chat's live primary.

    *ref* is a spawn ID or a chat ID. *sender_project_root* is the sender's project
    when it differs from this one; the sender's record is read from there.
    """

    normalized_ref = ref.strip()
    if not normalized_ref:
        _fail("spawn or chat id is required")

    project_root, _ = resolve_runtime_root_and_config(None)
    runtime_root = resolve_runtime_root(project_root)
    try:
        normalized_spawn_id = resolve_message_target(
            project_root, normalized_ref, runtime_root=runtime_root
        )
    except ValueError as exc:
        _fail(str(exc))
    if not (runtime_root / "spawns" / normalized_spawn_id).exists():
        _fail(f"spawn not found: {normalized_spawn_id}")
    socket_path = control_socket_path(runtime_root, normalized_spawn_id)

    normalized_message = message.strip()
    if not normalized_message:
        _fail("message is required")

    sender = _sender_record(
        resolve_runtime_root(sender_project_root) if sender_project_root else runtime_root,
        sender_spawn_id,
    )
    framed = framed_message(
        normalized_message,
        sender=sender,
        sender_project_root=sender_project_root,
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
