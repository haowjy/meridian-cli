"""Who a message says it is from, and the address that answers it.

One builder serves the frame a receiving session reads and the agy never-delivered notice, so
neither can print an address the machine it runs on can't resolve. A sender on another machine
is looked up by its machine ID in this machine's saved list, never by the name in the request:
the address is written with the name saved here, which is the one that resolves here.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.core.refs import is_chat_id
from meridian.lib.machines.caller import MachineCaller
from meridian.lib.state.saved_machines import (
    SavedMachine,
    SavedMachinesError,
    read_saved_machines,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sender:
    """What the frame says: who sent it, and the command that answers (or why none does)."""

    where: str
    reply: str | None = None
    """The whole command, beginning ``meridian message``, with ``"..."`` for the text."""
    no_reply: str = "it has no reply route here"


def _where(spawn_id: str, chat_id: str | None) -> str:
    return f"spawn {spawn_id}" + (f" (chat {chat_id})" if chat_id and chat_id != spawn_id else "")


def local_sender(
    spawn_id: str | None,
    *,
    chat_id: str | None,
    reply_id: str | None,
    claimed: bool,
    reply_project: Path | None,
) -> Sender | None:
    """A sender on this machine. *reply_id* is its current chat, else its spawn, and is ``None``
    when nothing would resolve a message sent back; *reply_project* is its project when that
    differs from the receiver's, which an ID that isn't machine-wide needs as ``-C``."""

    if spawn_id is None:
        return None
    where = _where(spawn_id, chat_id)
    if reply_id is None:
        return Sender(where)
    via = f"-C {shlex.quote(reply_project.as_posix())} " if reply_project and not claimed else ""
    return Sender(where, f'meridian message {reply_id} {via}"..."')


def reply_tokens(
    machine: SavedMachine, reply_id: str, *, claimed: bool, project_root: str | None
) -> list[str] | None:
    """The words after ``meridian message`` that answer a session on *machine*, or ``None`` when
    the session's ID isn't machine-wide and its project isn't known."""

    if claimed:
        return [f"{machine.name}/{reply_id}"]
    if project_root is None:
        return None
    return [reply_id, "--machine", machine.name, "-C", project_root]


def saved_machine_by_id(machine_id: str) -> SavedMachine | None:
    """The saved machine with this ID; a list that can't be used is a warning, not a failure:
    it must stop a reply, never a delivery."""

    try:
        return next((entry for entry in read_saved_machines() if entry.id == machine_id), None)
    except SavedMachinesError as exc:
        logger.warning("%s: no way back to the sender while the saved list is unusable", exc)
        return None


def machine_sender(caller: MachineCaller) -> Sender | None:
    """A sender on another machine, from the request's ``caller`` block."""

    if caller.spawn_id is None:
        return None
    saved = saved_machine_by_id(caller.machine_id)
    label = saved.name if saved is not None else caller.machine
    chat = caller.reply_id if caller.reply_id and is_chat_id(caller.reply_id) else None
    where = f"{_where(caller.spawn_id, chat)} on {label}"
    if not caller.can_reply or caller.reply_id is None:
        return Sender(where)
    if saved is None:
        return Sender(
            where,
            no_reply=(
                f"there is no way back from here: run `meridian machine add <SSH target of "
                f"{caller.machine}>` to make one"
            ),
        )
    tokens = reply_tokens(
        saved, caller.reply_id, claimed=caller.claimed, project_root=caller.project_root
    )
    if tokens is None:
        return Sender(where)
    return Sender(where, "meridian message " + " ".join(shlex.quote(t) for t in tokens) + ' "..."')


__all__ = ["Sender", "local_sender", "machine_sender", "reply_tokens", "saved_machine_by_id"]
