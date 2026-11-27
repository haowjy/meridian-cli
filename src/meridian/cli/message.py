"""``meridian message``: send a message to a running session or a person.

``send_message`` returns a result and prints nothing; ``run_message`` prints it. The
deprecated ``spawn message`` runs the same ``run_message``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

from cyclopts import Parameter

from meridian.cli.app_tree import message_app
from meridian.cli.invocation import get_global_options
from meridian.cli.machine_forward import base_caller, other_machine
from meridian.lib.core.caller import caller_identity, caller_project_root
from meridian.lib.core.caller_boundary import (
    SANDBOX_BLOCKED_SENTENCE,
    inside_launcher_sandbox,
)
from meridian.lib.core.delivery import SEND_TIMEOUT_SECONDS
from meridian.lib.core.process_start import starting_cwd, starting_env
from meridian.lib.core.refs import RefAmbiguous, RefNotFound, is_chat_id, is_spawn_id
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.harness.injected_marker import MARKER
from meridian.lib.inbox.caller import resolve_caller
from meridian.lib.inbox.filing import FilingOptions, file_from_command
from meridian.lib.inbox.store import MESSAGE_ID, InboxError
from meridian.lib.machines.call import MESSAGE_LIMIT_SECONDS, call_machine
from meridian.lib.machines.caller import machine_call_caller
from meridian.lib.machines.resolve import MachineError
from meridian.lib.machines.results import Answered, NotSent, Unanswered
from meridian.lib.message.address import (
    AddressError,
    InboxAddress,
    MachineAddress,
    PersonAddress,
    local_machine_name,
    parse_address,
)
from meridian.lib.message.people import deliver_to_person
from meridian.lib.message.reply import Sender, local_sender, machine_sender
from meridian.lib.message.result import (
    Delivered,
    Failed,
    Filed,
    MessageResult,
    Receiver,
    delivery_kind,
    error_kind,
    print_result,
)
from meridian.lib.ops.locate import locate_run, run_is_live
from meridian.lib.ops.runtime import resolve_project_authority, resolve_runtime_root_for_read
from meridian.lib.ops.spawn.idle import row_idle
from meridian.lib.state.machine_ids import claim_root
from meridian.lib.state.runtime_root import runtime_root_project_id
from meridian.lib.state.saved_machines import SavedMachine
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import read_derived
from meridian.lib.streaming.control_socket import MAX_REQUEST_BYTES, control_socket_path
from meridian.lib.utils.json_shapes import json_object

# Above the server's send bound, so its failure text reaches the sender.
_RESPONSE_TIMEOUT_SECONDS = SEND_TIMEOUT_SECONDS + 2.0
_ENDPOINT_DISCOVERY_TIMEOUT_SECONDS = 30.0
_ENDPOINT_DISCOVERY_POLL_SECONDS = 0.1

MESSAGE_ADDRESS_HELP: Final = (
    "Required. Where the message goes: a session's spawn ID or chat ID (that chat's running "
    "session), `<machine>/<id>` for a session on a saved machine (`meridian machine list`), "
    "`@person` for a person, or `inbox` to leave it in this machine's inbox for a session to "
    "take (`<machine>/inbox` for a saved machine's). A new ID works from any directory; "
    "`-C <repo>` reaches an ID issued before "
    "machine-wide IDs in another project. For an old ID on another machine: "
    "`--machine <name> -C <path on that machine>`, after the address."
)
MESSAGE_WAKE_HELP: Final = (
    "Deliver to a session whose prompt cache has gone cold. Without it, a message to a session "
    "that is idle past its cache's expiry is held and fails with kind `cold`: waking it "
    "re-reads the whole conversation. Best effort: a session that reports no idle state is "
    "never held. Not valid with an `@` address."
)
MESSAGE_TEXT_HELP: Final = (
    "Message text, or `-` to read it from stdin. Put long content in a file and send its path; "
    "for another machine send the content, or put it in the work item's directory: a path "
    "means nothing there."
)
_INBOX_ONLY: Final = "Only with an `inbox` address."
MESSAGE_PROJECT_ID_HELP: Final = (
    "The project the message is for, by project ID: sessions in other projects don't see it. "
    f"Only from a process no session runs; a session's message carries its own. {_INBOX_ONLY}"
)
MESSAGE_REPLY_TO_HELP: Final = (
    "The address an answer goes to (a session ID, `<machine>/<id>` or an @person) when it is "
    f"not the sender. {_INBOX_ONLY}"
)
MESSAGE_VIA_PROGRAM_HELP: Final = (
    f"The outside program that passes the message on, shown as its claim. {_INBOX_ONLY}"
)
MESSAGE_VIA_REF_HELP: Final = (
    "That program's own ID for the message. The same program and ref filed twice is one "
    f"inbox message. {_INBOX_ONLY}"
)
MESSAGE_VIA_SENDER_HELP: Final = f"Who that program says wrote the message. {_INBOX_ONLY}"


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


def framed_message(message: str, sender: Sender | None = None) -> str:
    """Open the message with its sender and, when it exists, a way to answer.

    A session that receives a message otherwise cannot tell which session sent it, or where
    to reply. The frame is text, not wire metadata: every harness's delivery path carries it
    unchanged. *sender* is ``None`` when the message has no sender identity.
    """

    if sender is None:
        provenance = f"{MARKER} Message with no sender identity; it has no reply address."
    elif sender.reply is None:
        provenance = f"{MARKER} Message from {sender.where}; {sender.no_reply}."
    else:
        provenance = f"{MARKER} Message from {sender.where}. To answer, run `{sender.reply}`."
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


def _local_sender(
    sender_spawn_id: str | None,
    *,
    sender_root: Path | None,
    runtime_root: Path,
    reply_project: Path,
) -> Sender | None:
    """The sender on this machine: its record names its current chat, the ID to answer."""

    sender = _sender_record(sender_root, sender_spawn_id)
    can_reply = (
        sender is not None and sender_root is not None and _can_receive_reply(sender, sender_root)
    )
    # The sender's current chat: after a /clear the entry chat is a past session.
    reply_id = (sender.continue_chat_id or sender.id) if sender is not None and can_reply else None
    return local_sender(
        sender_spawn_id,
        chat_id=sender.continue_chat_id if sender is not None else None,
        reply_id=reply_id,
        claimed=reply_id is not None and claim_root(reply_id) is not None,
        reply_project=reply_project if sender_root != runtime_root else None,
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

    project_root = resolve_project_authority().project_root
    current_root = resolve_runtime_root_for_read(project_root)
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
    if target.chat_id is not None and not run_is_live(target):
        return Failed("not_running", f"chat {target.chat_id} has no running session", receiver)
    receiver = dataclasses.replace(receiver, spawn_id=spawn_id, chat_id=target.chat_id)

    if not wake and (cold := _held_if_cold(runtime_root, spawn_id, receiver)) is not None:
        return cold

    sandbox = inside_launcher_sandbox()
    remote = machine_call_caller()
    if remote is not None:
        sender = machine_sender(remote)
    else:
        sender = _local_sender(
            sender_spawn_id,
            sender_root=(
                resolve_runtime_root_for_read(sender_project_root)
                if sender_project_root is not None
                else current_root
            ),
            runtime_root=runtime_root,
            reply_project=sender_project_root or project_root,
        )
    framed = framed_message(text, sender)

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
            return Failed("sandboxed", SANDBOX_BLOCKED_SENTENCE, receiver)
        else:
            visible = True
        if visible:
            break
        if sandbox is True:
            # A sandbox with a private /tmp hides the socket file; waiting would end in no_route.
            return Failed("sandboxed", SANDBOX_BLOCKED_SENTENCE, receiver)
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
                no_route += f"; {SANDBOX_BLOCKED_SENTENCE}"
            return Failed("no_route", no_route, receiver)
        await asyncio.sleep(min(_ENDPOINT_DISCOVERY_POLL_SECONDS, remaining))

    # The server limit applies to the encoded line (JSON escapes non-ASCII to 6 bytes).
    request: dict[str, object] = {"type": "user_message", "text": framed}
    if remote is not None:
        if remote.spawn_id is not None and remote.project_root is not None:
            request["sender"] = {
                "spawn_id": remote.spawn_id,
                "project_root": remote.project_root,
                "machine_id": remote.machine_id,
                "reply_id": remote.reply_id,
                "claimed": remote.claimed,
            }
    elif sender_spawn_id is not None:
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
        return Failed("sandboxed", SANDBOX_BLOCKED_SENTENCE, receiver)
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        if sandbox is True:
            return Failed("sandboxed", SANDBOX_BLOCKED_SENTENCE, receiver)
        text_ = (
            f"connection refused: spawn {spawn_id} is not running"
            if isinstance(exc, ConnectionRefusedError)
            else f"spawn not running: {spawn_id} control socket disappeared"
        )
        if sandbox is None:
            text_ += f"; {SANDBOX_BLOCKED_SENTENCE}"
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


def machine_caller_block() -> dict[str, object]:
    """What the receiving machine's frame needs about the sender, which is a file on this one.

    ``reply_id`` is the sender's current chat, else its spawn (not the environment: after a
    ``/clear`` the entry chat is a past session). Everything past the machine is absent when
    the caller is a person at a terminal.
    """

    block = base_caller()
    identity = caller_identity()
    if identity is None:
        return block
    record = read_derived(identity.runtime_root, identity.spawn_id)
    reply_id = (record.continue_chat_id or record.id) if record is not None else identity.spawn_id
    block.update(
        {
            "spawn_id": str(identity.spawn_id),
            "reply_id": reply_id,
            "claimed": claim_root(reply_id) is not None,
            "can_reply": record is not None and _can_receive_reply(record, identity.runtime_root),
        }
    )
    project_root = caller_project_root()
    if project_root is not None:
        block["project_root"] = project_root.as_posix()
    project_id = runtime_root_project_id(identity.runtime_root)
    if project_id is not None:
        block["project_id"] = project_id
    return block


def _delivered_or_failed(
    answer: Answered, receiver: Receiver, machine: str, to: str
) -> MessageResult:
    """The other side's ``message --json`` object as a result; anything else it printed means
    the message may have arrived. *to* is how the text output names the receiver."""

    def uncertain(why: str) -> Failed:
        return Failed(
            "maybe_delivered",
            f"{why}; the message may have been delivered and was not resent",
            receiver,
        )

    if answer.timed_out or answer.truncated:
        return uncertain(f"{machine} timed out answering")
    parsed = None
    for line in reversed(answer.stdout.strip().splitlines()):
        try:
            parsed = json_object(json.loads(line))
        except ValueError:
            continue
        break
    if parsed is None or not isinstance(parsed.get("ok"), bool):
        detail = answer.stderr.strip().splitlines()[-1:] or [f"exit status {answer.exit}"]
        return uncertain(f"{machine} gave no usable answer ({detail[0]})")
    # They go into this side's output and an offer's record: only an ID's shape is let through.
    ids = {
        key: value
        for key, shaped in (("spawn_id", is_spawn_id), ("chat_id", is_chat_id))
        if isinstance(value := parsed.get(key), str) and shaped(value)
    }
    receiver = dataclasses.replace(receiver, **ids)
    if parsed["ok"] is True and parsed.get("delivery") == "inbox":
        # It goes into this side's output: only an ID's shape is let through.
        filed = parsed.get("message")
        if not isinstance(filed, str) or MESSAGE_ID.fullmatch(filed) is None:
            return uncertain(f"{machine} filed the message and gave no usable ID")
        return Filed(filed, receiver, machine)
    if parsed["ok"] is True:
        note = parsed.get("note")
        return Delivered(
            delivery_kind(parsed.get("delivery")),
            to,
            note if isinstance(note, str) else None,
            receiver,
        )
    error = json_object(parsed.get("error")) or {}
    text = error.get("text")
    return Failed(
        error_kind(error.get("kind"), unknown="maybe_delivered"),
        text.strip() if isinstance(text, str) and text.strip() else "unknown error",
        receiver,
    )


async def _send_to_machine(
    machine: SavedMachine,
    address: str,
    flags: list[str],
    text: str,
    receiver: Receiver,
    *,
    caller: dict[str, object],
    remote_directory: str | None,
) -> MessageResult:
    """Deliver through ``machine run`` on a saved machine: that machine's own ``message`` does the
    delivery, so its result is read back, never re-derived here. *address* is the address as
    that machine takes it (a bare ID, or ``inbox``). The text goes on stdin, never in ``argv``:
    one argument is limited to 128 KiB and a message may be 16 MiB."""

    argv = ["message", address, "-", "--json", *flags]
    if remote_directory is not None:
        argv += ["-C", remote_directory]
    result = await asyncio.to_thread(
        call_machine,
        machine.ssh,
        argv,
        text,
        MESSAGE_LIMIT_SECONDS,
        machine.id,
        caller=caller,
        label=machine.name,
    )
    match result:
        case NotSent(kind, why):
            return Failed(kind, why, receiver)
        case Unanswered(why):
            return Failed(
                "maybe_delivered",
                f"{why}; the message may have been delivered and was not resent",
                receiver,
            )
        case Answered():
            return _delivered_or_failed(result, receiver, machine.name, f"{machine.name}/{address}")


async def send_message(
    ref: str,
    message: str,
    *,
    sender_spawn_id: str | None = None,
    sender_project_root: Path | None = None,
    machine: SavedMachine | None = None,
    remote_directory: str | None = None,
    directory_explicit: bool = False,
    wake: bool = False,
    filing: FilingOptions | None = None,
) -> MessageResult:
    """Send *message* to the receiver *ref* names and return what happened.

    *ref* is a spawn ID, a chat ID, ``<machine>/<id>``, an ``@person``, ``inbox`` or
    ``<machine>/inbox``.
    *machine* is the other machine ``--machine`` named, *remote_directory* the ``-C`` that
    goes with it, and *directory_explicit* that ``-C`` was applied here.
    *sender_project_root* is the sender's project when it differs from this one; the sender's
    record is read from there. *filing* holds the flags of the ``inbox`` address. Nothing is
    printed.
    """

    address = ref.strip()
    text = message.strip()
    receiver = Receiver(address=address or None)
    if not address:
        return Failed(
            "usage",
            "an address is required: a spawn ID, a chat ID, `<machine>/<id>`, an @person, "
            "`inbox` or `<machine>/inbox`",
            receiver,
        )
    if not text:
        return Failed("usage", "message is required", receiver)
    try:
        target = (
            machine
            if address.startswith("@")
            else other_machine(
                [address], option_machine=machine, directory_explicit=directory_explicit
            )
        )
        # Only now is it known which machine the address is on: an unknown one names none.
        receiver = dataclasses.replace(
            receiver, machine=target.name if target is not None else local_machine_name()
        )
        parsed = parse_address(address, machine=target)
        if isinstance(parsed, InboxAddress):
            if wake:
                raise AddressError("usage", "--wake belongs to a session address, not the inbox")
            filing = filing or FilingOptions()
            try:
                if parsed.machine is None:
                    return file_from_command(
                        text,
                        filing,
                        receiver,
                        sender_spawn_id=sender_spawn_id,
                        sender_project_root=sender_project_root,
                    )
                # Who may file is settled here, as for a message filed here: a session
                # with no record or no usable project is refused before anything connects.
                resolve_caller(sender_spawn_id, sender_project_root)
            except InboxError as exc:
                return Failed(exc.kind, str(exc), receiver)
            except PermissionError:
                if inside_launcher_sandbox() is False:
                    raise
                return Failed("sandboxed", SANDBOX_BLOCKED_SENTENCE, receiver)
            return await _send_to_machine(
                parsed.machine,
                "inbox",
                filing.argv(),
                text,
                receiver,
                caller=machine_caller_block(),
                remote_directory=remote_directory,
            )
        if filing is not None and filing.given():
            raise AddressError(
                "usage", f"{', '.join(filing.given())} belongs to the `inbox` address"
            )
        if isinstance(parsed, PersonAddress):
            if wake:
                raise AddressError("usage", "--wake belongs to a session address, not an @ address")
            return await deliver_to_person(
                parsed.address, text, receiver, env=starting_env(), cwd=starting_cwd()
            )
        if isinstance(parsed, MachineAddress):
            return await _send_to_machine(
                parsed.machine,
                parsed.id,
                ["--wake"] if wake else [],
                text,
                receiver,
                caller=machine_caller_block(),
                remote_directory=remote_directory,
            )
        return await _send_to_session(
            parsed.id,
            text,
            receiver,
            sender_spawn_id=sender_spawn_id,
            sender_project_root=sender_project_root,
            wake=wake,
        )
    except (AddressError, MachineError) as exc:
        return Failed(exc.kind, exc.text, receiver)
    except Exception as exc:
        # Only what happens before a route writes: each route answers its own failures after.
        return Failed("failed", str(exc) or type(exc).__name__, receiver)


def run_message(
    ref: str, message: str, wake: bool = False, filing: FilingOptions | None = None
) -> None:
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
                machine=options.machine,
                remote_directory=options.remote_directory,
                directory_explicit=options.directory_explicit,
                wake=wake,
                filing=filing,
            )
        )
    raise SystemExit(print_result(result, json_mode=json_mode))


@message_app.default
def cmd_message(
    ref: Annotated[str, Parameter(show_default=False, help=MESSAGE_ADDRESS_HELP)] = "",
    message: Annotated[str, Parameter(help=MESSAGE_TEXT_HELP)] = "",
    *,
    wake: Annotated[bool, Parameter(name="--wake", help=MESSAGE_WAKE_HELP)] = False,
    project_id: Annotated[
        str | None, Parameter(name="--project-id", help=MESSAGE_PROJECT_ID_HELP)
    ] = None,
    reply_to: Annotated[
        str | None, Parameter(name="--reply-to", help=MESSAGE_REPLY_TO_HELP)
    ] = None,
    via_program: Annotated[
        str | None, Parameter(name="--via-program", help=MESSAGE_VIA_PROGRAM_HELP)
    ] = None,
    via_ref: Annotated[str | None, Parameter(name="--via-ref", help=MESSAGE_VIA_REF_HELP)] = None,
    via_sender: Annotated[
        str | None, Parameter(name="--via-sender", help=MESSAGE_VIA_SENDER_HELP)
    ] = None,
) -> None:
    """Send a message to a running session or a person, or file it in the machine's inbox."""

    run_message(
        ref, message, wake, FilingOptions(project_id, reply_to, via_program, via_ref, via_sender)
    )


__all__ = [
    "MESSAGE_ADDRESS_HELP",
    "MESSAGE_TEXT_HELP",
    "MESSAGE_WAKE_HELP",
    "cmd_message",
    "framed_message",
    "machine_caller_block",
    "run_message",
    "send_message",
]
