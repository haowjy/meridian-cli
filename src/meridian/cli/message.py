"""``meridian message``: send a message to a running session or a person.

``send_message`` returns a result and prints nothing; ``run_message`` prints it. The
deprecated ``spawn message`` runs the same ``run_message``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

from cyclopts import Parameter

from meridian.cli.app_tree import message_app
from meridian.cli.invocation import get_global_options
from meridian.lib.core.caller_boundary import inside_launcher_sandbox
from meridian.lib.core.delivery import SEND_TIMEOUT_SECONDS
from meridian.lib.core.process_start import starting_cwd, starting_env
from meridian.lib.core.refs import RefAmbiguous, RefNotFound
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.harness.injected_marker import MARKER
from meridian.lib.message.address import (
    AddressError,
    PersonAddress,
    local_machine_name,
    parse_address,
)
from meridian.lib.message.people import deliver_to_person
from meridian.lib.message.result import (
    Delivered,
    Failed,
    MessageResult,
    Receiver,
    delivery_kind,
    error_kind,
    print_result,
)
from meridian.lib.ops.locate import locate_run
from meridian.lib.ops.runtime import (
    resolve_runtime_root,
    resolve_runtime_root_and_config,
    resolve_runtime_root_for_read,
)
from meridian.lib.ops.spawn.idle import row_idle
from meridian.lib.state.machine_ids import claim_root
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import read_derived
from meridian.lib.streaming.control_socket import MAX_REQUEST_BYTES, control_socket_path
from meridian.lib.utils.json_shapes import json_object

# Above the server's send bound, so its failure text reaches the sender.
_RESPONSE_TIMEOUT_SECONDS = SEND_TIMEOUT_SECONDS + 2.0
_ENDPOINT_DISCOVERY_TIMEOUT_SECONDS = 30.0
_ENDPOINT_DISCOVERY_POLL_SECONDS = 0.1

_SANDBOX_SENTENCE: Final = (
    "this command ran inside the session's sandbox, which blocks the connection. Run it as "
    "a command of its own that begins `meridian message` (no `cd … &&`, no pipe, no `VAR=1` "
    "in front). A session started before Meridian 0.28 has no grant for that: restart it, "
    'or use `meridian spawn message <address> --message "…"`, which works through 0.28.x.'
)

MESSAGE_ADDRESS_HELP: Final = (
    "Required. Where the message goes: a session's spawn ID or chat ID (that chat's running "
    "session), or `@person` for a person. A new ID works from any directory; `-C <repo>` "
    "reaches an ID issued before machine-wide IDs in another project."
)
MESSAGE_WAKE_HELP: Final = (
    "Deliver to a session whose prompt cache has gone cold. Without it, a message to a session "
    "that is idle past its cache's expiry is held and fails with kind `cold`: waking it "
    "re-reads the whole conversation. Best effort: a session that reports no idle state is "
    "never held. Not valid with an `@` address."
)
MESSAGE_TEXT_HELP: Final = (
    "Message text, or `-` to read it from stdin. Put long content in a file and send its path."
)


def _held_if_cold(runtime_root: Path, spawn_id: str, receiver: Receiver) -> Failed | None:
    """``Failed("cold")`` when the session is idle past its cache's expiry, else ``None``.

    The same ``idle`` ``spawn list --json`` shows; a session with no idle data is not cold,
    and neither is one in a turn right now (``spawn_idle`` reads that as no idle stretch).
    Runs before any byte is sent, so ``cold`` always means nothing was delivered.
    """

    record = read_derived(runtime_root, spawn_id)
    if record is None:
        return None
    idle = row_idle(runtime_root, record)
    if idle is None or idle.warm_until is None:
        return None
    now = datetime.now(UTC)
    if datetime.fromisoformat(idle.warm_until) > now:
        return None
    idle_for = _duration_text((now - datetime.fromisoformat(idle.since)).total_seconds())
    cost = record.total_usage.total_cost_usd
    cost_text = f"${cost:.2f}" if cost else "nothing recorded"
    chat = receiver.chat_id or record.continue_chat_id or spawn_id
    receiver = dataclasses.replace(receiver, chat_id=chat if chat != spawn_id else None)
    return Failed(
        "cold",
        f"spawn {spawn_id} is cold: idle for {idle_for}, past its prompt cache's expiry. "
        f"Model {record.model or 'unknown'}; cost so far {cost_text}. Waking it re-reads the whole "
        "conversation. Nothing was sent. Send again with --wake to deliver anyway, or start "
        f"a new session from its context with `--from {chat}`.",
        receiver,
    )


def _duration_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    return f"{minutes // 60} h {minutes % 60} min"


def framed_message(
    message: str,
    *,
    sender_spawn_id: str | None = None,
    sender: SpawnRecord | None = None,
    can_reply: bool = False,
    reply_project: Path | None = None,
) -> str:
    """Open the message with its sender and, when it exists, a way to answer.

    A session that receives a message otherwise cannot tell which session sent
    it, or where to reply. The frame is text, not wire metadata: every harness's
    delivery path carries it unchanged. *sender* is the sender's own record, absent
    when its state can't be read from here; *can_reply* is whether a post back
    would resolve a route; *reply_project* is the sender's project, used only when
    the sender lives in another project and its ID is not machine-wide, which is when
    the reply needs ``-C`` (after the address, so the line begins ``meridian message``).
    """

    if sender_spawn_id is None:
        provenance = f"{MARKER} Message with no sender identity; it has no reply address."
    elif sender is None or not can_reply:
        where = f"spawn {sender_spawn_id}"
        if sender is not None and sender.continue_chat_id:
            where += f" (chat {sender.continue_chat_id})"
        provenance = f"{MARKER} Message from {where}; it has no reply route here."
    else:
        # The sender's current chat: after a /clear the entry chat is a past session.
        address = sender.continue_chat_id or sender.id
        via = ""
        if reply_project is not None and claim_root(address) is None:
            via = f"-C {shlex.quote(reply_project.as_posix())} "
        where = f"spawn {sender.id}"
        if sender.continue_chat_id:
            where += f" (chat {sender.continue_chat_id})"
        provenance = (
            f"{MARKER} Message from {where}. To answer, run "
            f'`meridian message {address} {via}"..."`.'
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


async def _send_and_receive(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    line: bytes,
) -> bytes:
    try:
        writer.write(line)
        await writer.drain()
        return await asyncio.wait_for(reader.readline(), timeout=_RESPONSE_TIMEOUT_SECONDS)
    finally:
        writer.close()
        await writer.wait_closed()


async def _send_to_session(
    session_id: str,
    text: str,
    receiver: Receiver,
    *,
    sender_spawn_id: str | None,
    sender_project_root: Path | None,
    wake: bool,
) -> MessageResult:
    """Deliver through a session's control socket.

    Up to the first written byte a failure has the kind of its cause. After it, any
    outcome without a parsed answer is ``maybe_delivered``: the session may have the
    message, and a script that read it as a plain failure would send it twice.
    """

    project_root, _ = resolve_runtime_root_and_config(None)
    current_root = resolve_runtime_root(project_root)
    try:
        target = locate_run(project_root, session_id)
    except RefAmbiguous as exc:
        return Failed("ambiguous", str(exc), receiver)
    except RefNotFound as exc:
        return Failed("not_found", str(exc), receiver)
    except ValueError as exc:
        return Failed("usage", str(exc), receiver)
    runtime_root = target.runtime_root
    spawn_id = str(target.spawn_id)
    if target.chat_id is not None and not target.occupant:
        owner = read_derived(runtime_root, target.spawn_id)
        if (
            owner is None
            or not is_active_spawn_status(owner.status)
            or (owner.current_chat_id not in (None, target.chat_id))
        ):
            return Failed("not_running", f"chat {target.chat_id} has no running session", receiver)
    receiver = dataclasses.replace(receiver, spawn_id=spawn_id, chat_id=target.chat_id)

    if not wake and (cold := _held_if_cold(runtime_root, spawn_id, receiver)) is not None:
        return cold

    sandbox = inside_launcher_sandbox()
    sender_root = (
        resolve_runtime_root_for_read(sender_project_root)
        if sender_project_root is not None
        else current_root
    )
    sender = _sender_record(sender_root, sender_spawn_id)
    framed = framed_message(
        text,
        sender_spawn_id=sender_spawn_id,
        sender=sender,
        can_reply=sender is not None
        and sender_root is not None
        and _can_receive_reply(sender, sender_root),
        reply_project=(sender_project_root or project_root)
        if sender_root != runtime_root
        else None,
    )

    socket_path = control_socket_path(runtime_root, spawn_id)
    discovery_deadline = time.monotonic() + _ENDPOINT_DISCOVERY_TIMEOUT_SECONDS
    while True:
        record = read_derived(runtime_root, spawn_id)
        if record is None or not is_active_spawn_status(record.status):
            return Failed(
                "not_running", f"spawn not running: {spawn_id} has no control endpoint", receiver
            )
        if receiver.chat_id is None and record.continue_chat_id:
            receiver = dataclasses.replace(receiver, chat_id=record.continue_chat_id)
        # ``Path.exists()`` reads any OSError as "absent" from Python 3.14 on, which would hide
        # a sandbox's denial; tell the two apart here.
        try:
            os.stat(socket_path)
        except FileNotFoundError:
            visible = False
        except PermissionError:
            return Failed("sandboxed", _SANDBOX_SENTENCE, receiver)
        else:
            visible = True
        if visible:
            break
        if sandbox is True:
            # A sandbox with a private /tmp hides the socket file; waiting would end in no_route.
            return Failed("sandboxed", _SANDBOX_SENTENCE, receiver)
        remaining = discovery_deadline - time.monotonic()
        if remaining <= 0:
            older = (
                "; a primary may have been started by an older build (relaunch it)"
                " or run without a message channel"
                if record.kind == "primary"
                else ""
            )
            no_route = f"spawn {spawn_id} has no message route{older}"
            if sandbox is None:
                no_route += f"; {_SANDBOX_SENTENCE}"
            return Failed("no_route", no_route, receiver)
        await asyncio.sleep(min(_ENDPOINT_DISCOVERY_POLL_SECONDS, remaining))

    # The server limit applies to the encoded line (JSON escapes non-ASCII to 6 bytes).
    request: dict[str, object] = {"type": "user_message", "text": framed}
    if sender_spawn_id is not None:
        request["sender"] = {
            "spawn_id": sender_spawn_id,
            "project_root": (sender_project_root or project_root).as_posix(),
        }
    line = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > MAX_REQUEST_BYTES:
        return Failed(
            "too_large",
            f"message is over the {MAX_REQUEST_BYTES // (1024 * 1024)} MiB limit",
            receiver,
        )

    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except PermissionError:
        return Failed("sandboxed", _SANDBOX_SENTENCE, receiver)
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        if sandbox is True:
            return Failed("sandboxed", _SANDBOX_SENTENCE, receiver)
        text_ = (
            f"connection refused: spawn {spawn_id} is not running"
            if isinstance(exc, ConnectionRefusedError)
            else f"spawn not running: {spawn_id} control socket disappeared"
        )
        if sandbox is None:
            text_ += f"; {_SANDBOX_SENTENCE}"
        return Failed("not_running", text_, receiver)

    def may_have_arrived(why: str) -> Failed:
        return Failed(
            "maybe_delivered",
            f"{why}; the message may have been delivered and was not resent",
            receiver,
        )

    # One try from the first written byte to a parsed answer: whatever goes wrong in it,
    # the session may have the message, so a failure nobody listed must not read as `failed`.
    try:
        response_data = await _send_and_receive(reader, writer, line)
        if not response_data:
            return may_have_arrived(f"spawn {spawn_id} closed the connection without an answer")
        parsed = json_object(json.loads(response_data.decode("utf-8")))
        if parsed is None:
            return may_have_arrived(f"invalid response from spawn {spawn_id}")
        ok = parsed.get("ok")
        if ok is True:
            # A server without ``delivery`` (an older build) does not say how far the
            # message got, so claim only that it was posted.
            note = parsed.get("note")
            return Delivered(
                delivery_kind(parsed.get("delivery")),
                f"spawn {spawn_id}",
                note if isinstance(note, str) else None,
                receiver,
            )
        if ok is not False:
            return may_have_arrived(f"invalid response from spawn {spawn_id}")
        # A server without ``kind`` is an older build, which can't tell a refusal from a
        # delivery that may have arrived; the only safe reading is the second.
        error_value = parsed.get("error")
        error_text = error_value.strip() if isinstance(error_value, str) else ""
        return Failed(
            error_kind(parsed.get("kind"), unknown="maybe_delivered"),
            error_text or "unknown error",
            receiver,
        )
    except TimeoutError:
        return may_have_arrived(f"timed out waiting for response from spawn {spawn_id}")
    except (ConnectionResetError, BrokenPipeError):
        return may_have_arrived(f"connection to spawn {spawn_id} was reset")
    except Exception as exc:
        # Includes a reply over asyncio's 64 KiB line limit and one that isn't JSON.
        return may_have_arrived(f"no usable answer from spawn {spawn_id} ({exc})")


async def send_message(
    ref: str,
    message: str,
    *,
    sender_spawn_id: str | None = None,
    sender_project_root: Path | None = None,
    machine: str | None = None,
    wake: bool = False,
) -> MessageResult:
    """Send *message* to the receiver *ref* names and return what happened.

    *ref* is a spawn ID, a chat ID, ``<machine>/<id>`` or an ``@person``.
    *sender_project_root* is the sender's project when it differs from this one; the
    sender's record is read from there. Nothing is printed.
    """

    address = ref.strip()
    text = message.strip()
    receiver = Receiver(address=address or None, machine=local_machine_name())
    if not address:
        return Failed(
            "usage", "an address is required: a spawn ID, a chat ID or an @person", receiver
        )
    if not text:
        return Failed("usage", "message is required", receiver)
    try:
        parsed = parse_address(address, machine=machine)
        if isinstance(parsed, PersonAddress):
            if wake:
                raise AddressError("usage", "--wake belongs to a session address, not an @ address")
            return await deliver_to_person(
                parsed.address, text, receiver, env=starting_env(), cwd=starting_cwd()
            )
        return await _send_to_session(
            parsed.id,
            text,
            receiver,
            sender_spawn_id=sender_spawn_id,
            sender_project_root=sender_project_root,
            wake=wake,
        )
    except AddressError as exc:
        return Failed(exc.kind, exc.text, receiver)
    except Exception as exc:
        # Only what happens before a route writes: each route answers its own failures after.
        return Failed("failed", str(exc) or type(exc).__name__, receiver)


def run_message(ref: str, message: str, machine: str | None = None, wake: bool = False) -> None:
    """The body of ``meridian message`` and of the deprecated ``spawn message``."""

    options = get_global_options()
    json_mode = options.output.format == "json"
    result: MessageResult
    try:
        text = sys.stdin.read() if message == "-" else message
    except UnicodeDecodeError as exc:
        result = Failed("usage", f"stdin is not UTF-8 text: {exc}", Receiver(address=ref.strip()))
    else:
        caller = options.caller
        result = asyncio.run(
            send_message(
                ref,
                text,
                sender_spawn_id=caller.spawn_id if caller is not None else None,
                sender_project_root=caller.project_root if caller is not None else None,
                machine=machine,
                wake=wake,
            )
        )
    raise SystemExit(print_result(result, json_mode=json_mode))


@message_app.default
def cmd_message(
    ref: Annotated[str, Parameter(show_default=False, help=MESSAGE_ADDRESS_HELP)] = "",
    message: Annotated[str, Parameter(help=MESSAGE_TEXT_HELP)] = "",
    *,
    machine: Annotated[str | None, Parameter(name="--machine", show=False)] = None,
    wake: Annotated[bool, Parameter(name="--wake", help=MESSAGE_WAKE_HELP)] = False,
) -> None:
    """Send a message to a running session or a person."""

    run_message(ref, message, machine, wake)


__all__ = [
    "MESSAGE_ADDRESS_HELP",
    "MESSAGE_TEXT_HELP",
    "MESSAGE_WAKE_HELP",
    "cmd_message",
    "framed_message",
    "run_message",
    "send_message",
]
