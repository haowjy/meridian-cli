"""Offering a waiting message to a running primary session, and whether a session that
holds a claim still runs.

The send is the caller's: ``meridian message``'s delivery lives in the CLI today, and the
receiving side of a machine call brings its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meridian.lib.core.refs import RefAmbiguous, RefNotFound, parse_ref
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.inbox.caller import project_of_root
from meridian.lib.inbox.fields import shown
from meridian.lib.inbox.models import Claim, Message
from meridian.lib.inbox.store import InboxError, record_offer, waiting_message
from meridian.lib.message.result import Delivered, Failed, MessageResult
from meridian.lib.ops.locate import RunTarget, locate_run
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_status import read_derived

type Send = Callable[[str, str], MessageResult]
"""Deliver a notice (second argument) to a session ID (first) without waking a cold one."""


@dataclass(frozen=True)
class Offered:
    message_id: str
    delivered: Delivered
    to: str
    """The session as the offer record names it: its chat ID when it has one."""
    unrecorded: str | None = None
    """Why the offer record wasn't written though the notice went out."""


def _run_is_live(target: RunTarget) -> bool | None:
    """Whether the run a reference names is running in the chat it names; ``None`` when
    its record is gone. A chat ID names the run live in it, so a run that left the chat
    (a native ``/resume`` elsewhere) is not that chat's run any more."""

    record = read_derived(target.runtime_root, target.spawn_id)
    if record is None:
        return None
    return is_active_spawn_status(record.status) and (
        target.occupant or record.current_chat_id in (None, target.chat_id)
    )


def taker_running(claim: Claim, machine: str | None, project_root: Path) -> bool | None:
    """Whether the session that holds a claim is still running; ``None`` when that can't be
    told from here (a terminal, another machine, a record that is gone)."""

    if claim.by.kind != "session" or claim.by.id is None or claim.by.machine != machine:
        return None
    try:
        return _run_is_live(locate_run(project_root, claim.by.id))
    except (RefAmbiguous, RefNotFound, ValueError):
        return None


def notice(message_id: str, message: Message) -> str:
    """The fixed text an offered session receives. It never carries the message text."""

    about = [message_id, f"from {shown(message.sender.label())}"]
    if message.via is not None:
        about.append(shown(message.via.label()))
    line = ", ".join(about)
    if message.project is not None:
        line += f" (project {shown(message.project)})"
    return (
        f"A message is waiting in this machine's inbox and looks like yours: {line}.\n"
        f"`meridian inbox take {message_id}` claims and prints it. If it isn't yours, do "
        "nothing and it stays for someone else."
    )


def offer(
    message_id: str, session: str, *, project_root: Path, send: Send, now: datetime
) -> Offered:
    """Send *session* the notice for a waiting message and record the offer.

    Refuses, with nothing sent, a message that is taken, a session that isn't a primary
    (an offer says "claim it", and only a primary may), and a session in another project
    than the message's. What *send* answers decides the rest: only a delivered notice is
    recorded. *project_root* is where an ID issued before machine-wide IDs is looked up.
    """

    try:
        ref = parse_ref(session)
    except ValueError:
        ref = None
    if ref is None or ref.kind not in ("spawn", "chat", "machine"):
        raise InboxError(
            f"`{session}` is not a session: an offer goes to a spawn ID or chat ID", kind="usage"
        )
    # The spawn store is read before ``inbox.lock`` is taken: the lock is a leaf.
    # ``locate_run`` takes the reference as typed: it refuses another machine's name.
    try:
        target = locate_run(project_root, session)
    except RefAmbiguous as exc:
        raise InboxError(str(exc), kind="ambiguous") from exc
    except (RefNotFound, ValueError) as exc:
        raise InboxError(str(exc), kind="not_found") from exc
    record = spawn_store.get_spawn(target.runtime_root, target.spawn_id)
    if record is None or record.kind != "primary":
        what = f"a {record.kind} session" if record is not None else "unrecorded"
        raise InboxError(
            f"session {ref.value} is {what}, not a primary: only a primary session can "
            "take a message, so only a primary is offered one. Nothing was sent.",
            kind="refused",
        )
    session_project = project_of_root(target.runtime_root)

    message = waiting_message(message_id)
    if message.project is not None and message.project != session_project:
        raise InboxError(
            f"{message_id} is for project {message.project}; session {ref.value} is in "
            f"{'project ' + session_project if session_project else 'no project'}. "
            "Nothing was sent.",
            kind="refused",
        )

    result = send(ref.value, notice(message_id, message))
    if not isinstance(result, Delivered):
        reason = result.text if isinstance(result, Failed) else "it was not delivered"
        if isinstance(result, Failed) and result.kind == "cold":
            reason = f"an offer never wakes a cold session. {reason}"
        raise InboxError(
            f"no offer was recorded: {reason}",
            kind=result.kind if isinstance(result, Failed) else "failed",
        )

    to = result.receiver.chat_id or result.receiver.spawn_id or ref.value
    try:
        record_offer(message_id, to, now)
    except InboxError as exc:
        # The notice is out; the record is for `list` and for people, and grants nothing.
        return Offered(message_id, result, to, unrecorded=str(exc))
    return Offered(message_id, result, to)


__all__ = ["Offered", "Send", "offer", "taker_running"]
