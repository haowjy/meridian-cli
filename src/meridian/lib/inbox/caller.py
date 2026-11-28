"""Who is running an inbox command: the one answer its role, ``from``, taker and
visibility all come from.

The check shows that the session ID in the caller's environment has a record, not that
the caller is that session: it stops a confused child, not one that forges its identity.
A command another machine sent has no record here: its caller is what the request says,
and the machine that sent it has checked the role.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meridian.lib.core.caller import caller_runtime_root
from meridian.lib.inbox.fields import PROJECT_ID_RULE, is_project_id, is_safe_field
from meridian.lib.inbox.models import Party
from meridian.lib.inbox.store import InboxError
from meridian.lib.machines.caller import MachineCaller, is_machine_call, machine_call_caller
from meridian.lib.message.reply import saved_machine_by_id, way_back
from meridian.lib.state import spawn_store
from meridian.lib.state.machine_identity import MachineIdentity
from meridian.lib.state.machine_ids import claim_root
from meridian.lib.state.project_identity import ADHOC_PROJECT_ID_PREFIX
from meridian.lib.state.runtime_root import (
    derive_runtime_root_from_project,
    runtime_root_project_id,
)
from meridian.lib.state.user_paths import get_user_home
from meridian.plugin_api.types import SpawnKind


@dataclass(frozen=True)
class InboxCaller:
    """A session, or with ``spawn_id`` ``None`` a process no session claims: a person at
    a terminal, a daemon."""

    spawn_id: str | None = None
    chat_id: str | None = None
    """The session's own chat, from its record. ``MERIDIAN_CHAT_ID`` is not it: for a
    child that names the chat that launched it."""
    kind: SpawnKind | None = None
    project: str | None = None
    """The session's committed project ID (``project_of_root``). An ad-hoc project is none."""
    agent: str | None = None
    machine: str | None = None
    """The other machine the caller is on, by the name this machine knows it by; ``None``
    for a caller on this machine."""
    machine_id: str | None = None
    """That machine's ID, from its request."""
    reply_to: str | None = None
    """The address that answers a session on another machine from here, when there is one."""
    runtime_root: Path | None = None
    """Where a session of this machine has its record."""

    @property
    def is_terminal(self) -> bool:
        return self.spawn_id is None

    @property
    def may_change(self) -> bool:
        """``take``, ``release``, ``offer`` and ``--all``: a primary or a terminal. A request
        from another machine carries no role: that machine refused a child before it called."""

        return self.is_terminal or self.kind == "primary" or self.machine is not None

    def may_see(self, project: str | None, *, all_projects: bool = False) -> bool:
        """Whether a message filed under ``project`` is this caller's to list, read or take."""

        return self.is_terminal or project is None or project == self.project or all_projects

    def party(self, here: MachineIdentity | None) -> Party:
        """The caller as the inbox records it, *here* being this machine."""

        if self.machine is not None:
            name, machine_id = self.machine, self.machine_id
        else:
            name, machine_id = (here.name, here.id) if here is not None else (None, None)
        if self.is_terminal:
            return Party(kind="terminal", machine=name, machine_id=machine_id)
        return Party(kind="session", machine=name, machine_id=machine_id, id=self.chat_id)


def project_of_root(runtime_root: Path) -> str | None:
    """The committed project ID of the sessions recorded under a runtime root
    (``runtime_root_project_id``), or ``None`` for an ad-hoc project.

    A root outside ``projects/`` (``_MERIDIAN_RUNTIME_DIR``), or whose ID is outside what
    the inbox stores and prints, refuses: read as "no project", such a session's messages
    would be shown to every session.
    """

    project = runtime_root_project_id(runtime_root)
    if project is None:
        projects = (get_user_home() / "projects").resolve()
        if projects in runtime_root.resolve().parents:
            return None  # ad hoc
        named = "no project ID"
    elif is_project_id(project):
        return project
    else:
        named = f"project ID {project!r}"
    raise InboxError(
        f"the inbox can't tell which project the sessions under {runtime_root} belong "
        f"to: that directory gives {named}, and a project ID in the inbox is "
        f"{PROJECT_ID_RULE}. Nothing was changed.",
        kind="refused",
    )


def _from_machine(caller: MachineCaller | None) -> InboxCaller:
    """The caller of a command another machine sent: the session in the request's caller
    block, or with none a terminal of that machine."""

    if caller is None:
        raise InboxError(
            "the request from another machine names no usable caller. Nothing was changed.",
            kind="refused",
        )
    saved = saved_machine_by_id(caller.machine_id)
    machine = saved.name if saved is not None else caller.machine
    if caller.spawn_id is None:
        return InboxCaller(machine=machine, machine_id=caller.machine_id)
    project = caller.project_id
    if project is not None and project.startswith(ADHOC_PROJECT_ID_PREFIX):
        project = None
    if project is not None and not is_project_id(project):
        raise InboxError(
            f"the calling session's project ID {project!r} can't be used in the inbox: one "
            f"is {PROJECT_ID_RULE}. Nothing was changed.",
            kind="refused",
        )
    # A message's reply address is one word, so a way back that needs `-C` (an ID from
    # before machine-wide IDs) is none here.
    tokens = way_back(caller, saved) or []
    return InboxCaller(
        spawn_id=caller.spawn_id,
        chat_id=caller.reply_id or caller.spawn_id,
        project=project,
        machine=machine,
        machine_id=caller.machine_id,
        reply_to=tokens[0] if len(tokens) == 1 else None,
    )


def resolve_caller(spawn_id: str | None, project_root: Path | None) -> InboxCaller:
    """The caller named by the invocation's ``Caller`` (``get_global_options().caller``),
    which survives ``-C``: *project_root* is the caller's project when ``-C`` named
    another. An ID with no record refuses; it never falls back to a terminal. In a command
    another machine sent, the caller is the one its request names.
    """

    if is_machine_call():
        return _from_machine(machine_call_caller())
    if spawn_id is None:
        return InboxCaller()
    own = (
        derive_runtime_root_from_project(project_root)
        if project_root is not None
        else caller_runtime_root()
    )
    for root in (own, claim_root(spawn_id)):
        record = spawn_store.get_spawn(root, spawn_id) if root is not None else None
        if record is not None and root is not None:
            return InboxCaller(
                spawn_id=spawn_id,
                chat_id=record.continue_chat_id or spawn_id,
                kind=record.kind,
                project=project_of_root(root),
                # A label only, so a name the inbox can't store is left out, not refused.
                agent=record.agent if is_safe_field(record.agent or "") else None,
                runtime_root=root,
            )
    raise InboxError(
        f"this command runs as session {spawn_id} (MERIDIAN_SPAWN_ID), which has no record "
        "on this machine, so its role can't be checked. Nothing was changed.",
        kind="refused",
    )


__all__ = ["InboxCaller", "project_of_root", "resolve_caller"]
