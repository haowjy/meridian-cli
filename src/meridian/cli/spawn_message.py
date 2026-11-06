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

from meridian.lib.core.delivery import DeliveryKind
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.ops.locate import locate_run
from meridian.lib.ops.runtime import (
    resolve_runtime_root,
    resolve_runtime_root_and_config,
    resolve_runtime_root_for_read,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import read_derived
from meridian.lib.streaming.control_socket import MAX_REQUEST_BYTES, control_socket_path

# Above the 30 s harness request timeouts, so their failure text reaches the sender.
_RESPONSE_TIMEOUT_SECONDS = 35.0
_ENDPOINT_DISCOVERY_TIMEOUT_SECONDS = 30.0
_ENDPOINT_DISCOVERY_POLL_SECONDS = 0.1


def _fail(message: str) -> Never:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def framed_message(
    message: str,
    *,
    sender_spawn_id: str | None = None,
    sender: SpawnRecord | None = None,
    can_reply: bool = False,
    sender_project_root: Path | None = None,
) -> str:
    """Open the message with its sender and, when it exists, a way to answer.

    A session that receives a message otherwise cannot tell which session sent
    it, or where to reply. The frame is text, not wire metadata: every harness's
    delivery path carries it unchanged. *sender* is the sender's own record, absent
    when its state can't be read from here; *can_reply* is whether a post back
    would resolve a route; *sender_project_root* is set only when
    the sender lives in another project, which is when the reply needs ``-C``.
    """

    if sender_spawn_id is None:
        provenance = "[Meridian] Message with no sender identity; it has no reply address."
    elif sender is None or not can_reply:
        where = f"spawn {sender_spawn_id}"
        if sender is not None and sender.continue_chat_id:
            where += f" (chat {sender.continue_chat_id})"
        provenance = f"[Meridian] Message from {where}; it has no reply route here."
    else:
        # The sender's current chat: after a /clear the entry chat is a past session.
        address = sender.continue_chat_id or sender.id
        via = ""
        if sender_project_root is not None:
            via = f"-C {shlex.quote(sender_project_root.as_posix())} "
        where = f"spawn {sender.id}"
        if sender.continue_chat_id:
            where += f" (chat {sender.continue_chat_id})"
        provenance = (
            f"[Meridian] Message from {where}. To answer, run "
            f'`meridian {via}spawn message {address} --message "..."`.'
        )
    return f"{provenance}\n\n{message}"


def _sender_record(runtime_root: Path | None, sender_spawn_id: str | None) -> SpawnRecord | None:
    """Return the sender's own record, the source of its chat id and route.

    ``None`` when the sender's project has no runtime root or no such record; the
    frame then still names the sender, without a reply route.
    """

    if sender_spawn_id is None or runtime_root is None:
        return None
    return read_derived(runtime_root, sender_spawn_id)


def _can_receive_reply(sender: SpawnRecord, sender_root: Path) -> bool:
    """True when a message sent back to the sender would resolve a route.

    Checks the control socket file, not a connect: a dead socket on a live runner
    still passes. *sender_root* is the runtime root the sender's record came from.
    """

    return (
        is_active_spawn_status(sender.status)
        and control_socket_path(sender_root, sender.id).exists()
    )


def _message_target(project_root: Path, runtime_root: Path, ref: str) -> str:
    """Resolve a spawn ID or chat ID to the spawn that receives the message.

    A chat goes to the run live in it now: one that adopted it, else its owner while
    the owner is still in that chat.
    """

    try:
        if parse_ref(ref).kind == "selector":
            _fail(f"spawn message takes a spawn ID or chat ID, not {ref}")
        target = locate_run(project_root, ref, runtime_root=runtime_root)
    except ValueError as exc:
        _fail(str(exc))
    if target.chat_id is not None and not target.occupant:
        owner = read_derived(runtime_root, target.spawn_id)
        if (
            owner is None
            or not is_active_spawn_status(owner.status)
            or (owner.current_chat_id not in (None, target.chat_id))
        ):
            _fail(f"chat {target.chat_id} has no running session")
    return str(target.spawn_id)


async def _send_and_receive(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    line: bytes,
) -> bytes:
    """Send one encoded request line and receive the response, with proper cleanup."""
    try:
        writer.write(line)
        await writer.drain()
        return await asyncio.wait_for(reader.readline(), timeout=_RESPONSE_TIMEOUT_SECONDS)
    finally:
        writer.close()
        await writer.wait_closed()


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
    normalized_spawn_id = _message_target(project_root, runtime_root, normalized_ref)
    socket_path = control_socket_path(runtime_root, normalized_spawn_id)

    normalized_message = message.strip()
    if not normalized_message:
        _fail("message is required")

    sender_root = (
        resolve_runtime_root_for_read(sender_project_root)
        if sender_project_root is not None
        else runtime_root
    )
    sender = _sender_record(sender_root, sender_spawn_id)
    framed = framed_message(
        normalized_message,
        sender_spawn_id=sender_spawn_id,
        sender=sender,
        can_reply=sender is not None
        and sender_root is not None
        and _can_receive_reply(sender, sender_root),
        sender_project_root=sender_project_root,
    )

    discovery_deadline = time.monotonic() + _ENDPOINT_DISCOVERY_TIMEOUT_SECONDS
    while True:
        record = read_derived(runtime_root, normalized_spawn_id)
        if record is None or not is_active_spawn_status(record.status):
            _fail(f"spawn not running: {normalized_spawn_id} has no control endpoint")
        if socket_path.exists():
            break
        remaining = discovery_deadline - time.monotonic()
        if remaining <= 0:
            older = (
                "; a primary may have been started by an older build (relaunch it)"
                " or run without a message channel"
                if record.kind == "primary"
                else ""
            )
            _fail(f"spawn {normalized_spawn_id} has no message route{older}")
        await asyncio.sleep(min(_ENDPOINT_DISCOVERY_POLL_SECONDS, remaining))

    # The server limit applies to the encoded line (JSON escapes non-ASCII to 6 bytes).
    line = (
        json.dumps({"type": "user_message", "text": framed}, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(line) > MAX_REQUEST_BYTES:
        _fail(f"message is over the {MAX_REQUEST_BYTES // (1024 * 1024)} MiB limit")

    response_data = b""
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        response_data = await _send_and_receive(reader, writer, line)
    except TimeoutError:
        _fail(
            f"timed out waiting for response from spawn {normalized_spawn_id}; "
            "the message may have been delivered and was not resent"
        )
    except ConnectionRefusedError:
        _fail(f"connection refused: spawn {normalized_spawn_id} is not running")
    except FileNotFoundError:
        _fail(f"spawn not running: {normalized_spawn_id} control socket disappeared")
    except (ConnectionResetError, BrokenPipeError):
        _fail(
            f"connection to spawn {normalized_spawn_id} was reset; "
            "the message may not have been delivered and was not resent"
        )
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
        # A server without ``delivery`` (an older build) does not say how far the message
        # got, so claim only that it was posted.
        if parsed.get("delivery") == DeliveryKind.ACKNOWLEDGED:
            print(f"Message delivered to spawn {normalized_spawn_id}")
        else:
            print(f"Message posted to spawn {normalized_spawn_id} (no receipt)")
        note = parsed.get("note")
        if isinstance(note, str):
            print(note)
        return

    error_value = parsed.get("error")
    if isinstance(error_value, str) and error_value.strip():
        _fail(error_value.strip())
    _fail("unknown error")
