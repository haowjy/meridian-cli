"""Who is calling, for a command that `machine run` started on behalf of another machine.

``_MERIDIAN_MACHINE_CALL`` holds the request's ``caller`` block as JSON. Its presence is the
whole of "this command arrived from another machine": it is where the message frame gets its
sender, and it is what gives the command no current project. Import-cheap: startup reads it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from meridian.lib.core.refs import (
    is_chat_id,
    is_machine_id,
    is_machine_name,
    is_spawn_id,
    project_is_named,
)
from meridian.lib.utils.json_shapes import json_object

MACHINE_CALL_ENV: Final = "_MERIDIAN_MACHINE_CALL"


@dataclass(frozen=True)
class MachineCaller:
    """The calling machine and, when a session placed the call, that session."""

    machine: str
    machine_id: str
    spawn_id: str | None = None
    reply_id: str | None = None
    claimed: bool = False
    can_reply: bool = False
    project_root: str | None = None
    project_id: str | None = None


def _text(fields: Mapping[str, object], key: str) -> str | None:
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def _shaped(value: str | None, *, spawn: bool = False, chat: bool = False) -> str | None:
    """*value* when it has the shape of a spawn or chat ID: the request comes from the far side
    of a trust boundary and these go into a prompt and an argv."""

    if value is None:
        return None
    ok = (spawn and is_spawn_id(value)) or (chat and is_chat_id(value))
    return value if ok else None


def _path(value: str | None) -> str | None:
    return value if value is not None and value.isprintable() and value.startswith("/") else None


def _project_id(value: str | None) -> str | None:
    """A project ID names a directory under ``projects/``: one printable path component."""

    ok = value is not None and value.isprintable() and "/" not in value and value not in (".", "..")
    return value if ok and len(value or "") <= 255 else None


def parse_machine_caller(raw: object) -> MachineCaller | None:
    """The caller block of a request, ``None`` when it is absent or has no usable machine. A
    session field that has the wrong shape is dropped, not trusted."""

    fields = json_object(raw)
    if fields is None:
        return None
    machine, machine_id = _text(fields, "machine"), _text(fields, "machine_id")
    if (
        machine is None
        or machine_id is None
        or not is_machine_name(machine)
        or not is_machine_id(machine_id)
    ):
        return None
    return MachineCaller(
        machine=machine,
        machine_id=machine_id,
        spawn_id=_shaped(_text(fields, "spawn_id"), spawn=True),
        reply_id=_shaped(_text(fields, "reply_id"), spawn=True, chat=True),
        claimed=fields.get("claimed") is True,
        can_reply=fields.get("can_reply") is True,
        project_root=_path(_text(fields, "project_root")),
        project_id=_project_id(_text(fields, "project_id")),
    )


def is_machine_call(environ: Mapping[str, str] | None = None) -> bool:
    return bool((os.environ if environ is None else environ).get(MACHINE_CALL_ENV))


def machine_call_caller(environ: Mapping[str, str] | None = None) -> MachineCaller | None:
    raw = (os.environ if environ is None else environ).get(MACHINE_CALL_ENV)
    if not raw:
        return None
    try:
        return parse_machine_caller(json.loads(raw))
    except ValueError:
        return None


def is_machine_call_without_project() -> bool:
    """A command another machine asked for, with no ``-C``: it has no current project.

    ``ssh`` starts the command in the home directory, whose project is never what the caller
    meant. Readers of the runtime root answer "none", which ``ops/locate.py`` already treats
    as a project with no state: a claimed ID resolves by its claim, anything else needs ``-C``.
    """

    return is_machine_call() and not project_is_named()


def require_project_or_global(what: str) -> None:
    """Stop a command that would list "the current project" when another machine called it."""

    if is_machine_call_without_project():
        raise ValueError(
            f"{what} has no current project on the machine you called: add `--global`, or "
            "`-C <a path on that machine>`"
        )


def require_own_session(what: str) -> None:
    if is_machine_call():
        raise ValueError(
            f"{what} needs a session of its own, and a call to another machine has none: name "
            "the spawn"
        )


__all__ = [
    "MACHINE_CALL_ENV",
    "MachineCaller",
    "is_machine_call",
    "is_machine_call_without_project",
    "machine_call_caller",
    "parse_machine_caller",
    "require_own_session",
    "require_project_or_global",
]
