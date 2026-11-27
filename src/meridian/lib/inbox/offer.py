"""Offering a waiting message to a running primary session, and whether a session that
holds a claim still runs.

The send is the caller's: ``meridian message``'s delivery lives in the CLI today. So is
finding a session on another machine, which is a call to that machine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meridian.lib.core.refs import RefAmbiguous, RefNotFound, parse_ref
from meridian.lib.inbox.caller import project_of_root
from meridian.lib.inbox.fields import shown
from meridian.lib.inbox.models import Claim, Message
from meridian.lib.inbox.store import InboxError, record_offer, waiting_message
from meridian.lib.message.address import local_machine_name
from meridian.lib.message.result import Delivered, Failed, MessageResult
from meridian.lib.ops.locate import locate_run, run_is_live
from meridian.lib.state import spawn_store

type Send = Callable[[str, str], MessageResult]
"""Deliver a notice (second argument) to a session's address (first) without waking a cold
one."""


@dataclass(frozen=True)
class Target:
    """The session an offer goes to, as the machine it runs on reports it."""

    address: str
    """What ``Send`` takes and errors name: a bare ID, or ``<machine>/<id>``."""
    kind: str | None
    """Its record's kind; ``None`` when none is reported (another machine's `spawn show`
    leaves a child's out)."""
    project: str | None
    machine: str | None = None
    """The other machine it runs on; ``None`` for this one."""


type Find = Callable[[Message], Target]
"""Look the session up for an offer of this message; raises ``InboxError`` when it can't be
found."""


@dataclass(frozen=True)
class Offered:
    message_id: str
    delivered: Delivered
    to: str
    """The session as the offer record names it: its chat ID when it has one."""
    unrecorded: str | None = None
    """Why the offer record wasn't written though the notice went out."""


def taker_running(claim: Claim, machine: str | None, project_root: Path) -> bool | None:
    """Whether the session that holds a claim is still running; ``None`` when that can't be
    told from here (a terminal, another machine, a record that is gone)."""

    if claim.by.kind != "session" or claim.by.id is None or claim.by.machine != machine:
        return None
    try:
        return run_is_live(locate_run(project_root, claim.by.id))
    except (RefAmbiguous, RefNotFound, ValueError):
        return None


def notice(message_id: str, message: Message, holder: str | None = None) -> str:
    """The fixed text an offered session receives. It never carries the message text.
    *holder* is this machine's name when the session is on another one, which then has to
    name the machine to take the message."""

    name = f"{message_id} on {holder}" if holder else message_id
    about = [name, f"from {shown(message.sender.label())}"]
    if message.via is not None:
        about.append(shown(message.via.label()))
    line = ", ".join(about)
    if message.project is not None:
        line += f" (project {shown(message.project)})"
    where = f"the inbox of {holder}" if holder else "this machine's inbox"
    take = f"meridian inbox take {message_id}" + (f" --machine {holder}" if holder else "")
    return (
        f"A message is waiting in {where} and looks like yours: {line}.\n"
        f"`{take}` claims and prints it. If it isn't yours, do "
        "nothing and it stays for someone else."
    )


def local_target(session: str, project_root: Path) -> Target:
    """A session on this machine. *project_root* is where an ID issued before machine-wide
    IDs is looked up."""

    # ``locate_run`` takes the reference as typed: `<this machine>/<id>` is the bare ID.
    try:
        target = locate_run(project_root, session)
    except RefAmbiguous as exc:
        raise InboxError(str(exc), kind="ambiguous") from exc
    except (RefNotFound, ValueError) as exc:
        raise InboxError(str(exc), kind="not_found") from exc
    record = spawn_store.get_spawn(target.runtime_root, target.spawn_id)
    return Target(
        address=parse_ref(session).value,
        kind=record.kind if record is not None else None,
        project=project_of_root(target.runtime_root),
    )


def offer(message_id: str, session: str, *, find: Find, send: Send, now: datetime) -> Offered:
    """Send *session* the notice for a waiting message and record the offer.

    Refuses, with nothing sent, a message that is taken, a session that isn't a primary
    (an offer says "claim it", and only a primary may), and a session in another project
    than the message's. What *send* answers decides the rest: only a delivered notice is
    recorded.
    """

    try:
        ref = parse_ref(session)
    except ValueError:
        ref = None
    if ref is None or ref.kind not in ("spawn", "chat", "machine"):
        raise InboxError(
            f"`{session}` is not a session: an offer goes to a spawn ID, a chat ID or "
            "`<machine>/<id>`",
            kind="usage",
        )
    # The session is looked up with ``inbox.lock`` free: the lock is a leaf, and for a
    # session on another machine the lookup is a call there.
    message = waiting_message(message_id)
    target = find(message)
    if target.kind != "primary":
        what = f"a {target.kind} session, " if target.kind is not None else ""
        raise InboxError(
            f"session {target.address} is {what}not a primary: only a primary session can "
            "take a message, so only a primary is offered one. Nothing was sent.",
            kind="refused",
        )
    if message.project is not None and message.project != target.project:
        raise InboxError(
            f"{message_id} is for project {message.project}; session {target.address} is in "
            f"{'project ' + target.project if target.project else 'no project'}. "
            "Nothing was sent.",
            kind="refused",
        )

    holder = local_machine_name() if target.machine is not None else None
    result = send(target.address, notice(message_id, message, holder))
    if not isinstance(result, Delivered):
        reason = result.text if isinstance(result, Failed) else "it was not delivered"
        if isinstance(result, Failed) and result.kind == "cold":
            reason = f"an offer never wakes a cold session. {reason}"
        raise InboxError(
            f"no offer was recorded: {reason}",
            kind=result.kind if isinstance(result, Failed) else "failed",
        )

    to = result.receiver.chat_id or result.receiver.spawn_id or ref.value
    if target.machine is not None:
        to = f"{target.machine}/{to}"
    try:
        record_offer(message_id, to, now)
    except InboxError as exc:
        # The notice is out; the record is for `list` and for people, and grants nothing.
        return Offered(message_id, result, to, unrecorded=str(exc))
    return Offered(message_id, result, to)


__all__ = ["Find", "Offered", "Send", "Target", "local_target", "offer", "taker_running"]
