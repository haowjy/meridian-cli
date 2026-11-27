"""Who is running an inbox command: the one answer its role, ``from``, taker and
visibility all come from.

The check shows that the session ID in the caller's environment has a record, not that
the caller is that session: it stops a confused child, not one that forges its identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meridian.lib.core.caller import caller_runtime_root
from meridian.lib.inbox.fields import SAFE_FIELD_RULE, is_project_id, is_safe_field
from meridian.lib.inbox.models import Party
from meridian.lib.inbox.store import InboxError
from meridian.lib.state import spawn_store
from meridian.lib.state.machine_ids import claim_root
from meridian.lib.state.project_identity import ADHOC_PROJECT_ID_PREFIX
from meridian.lib.state.runtime_root import derive_runtime_root_from_project
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

    @property
    def is_terminal(self) -> bool:
        return self.spawn_id is None

    @property
    def may_change(self) -> bool:
        """``take``, ``release``, ``offer`` and ``--all``: a primary or a terminal."""

        return self.is_terminal or self.kind == "primary"

    def may_see(self, project: str | None, *, all_projects: bool = False) -> bool:
        """Whether a message filed under ``project`` is this caller's to list, read or take."""

        return self.is_terminal or project is None or project == self.project or all_projects

    def party(self, machine: str | None) -> Party:
        if self.is_terminal:
            return Party(kind="terminal", machine=machine)
        return Party(kind="session", machine=machine, id=self.chat_id)


def project_of_root(runtime_root: Path) -> str | None:
    """The committed project ID of the sessions recorded under a runtime root, or ``None``
    for an ad-hoc project. The one definition of a session's project.

    A root is ``projects/<project ID>`` and an ID may contain ``/``. A root that is
    somewhere else (``_MERIDIAN_RUNTIME_DIR``), or whose ID is outside what the inbox
    stores and prints, refuses: read as "no project", such a session's messages would be
    shown to every session.
    """

    try:
        relative = runtime_root.resolve().relative_to((get_user_home() / "projects").resolve())
    except ValueError:
        relative = None
    project = relative.as_posix() if relative is not None and relative.parts else None
    if project is not None and project.startswith(ADHOC_PROJECT_ID_PREFIX):
        return None
    if project is None or not is_project_id(project):
        named = f"project ID {project!r}" if project is not None else "no project ID"
        raise InboxError(
            f"the inbox can't tell which project the sessions under {runtime_root} belong "
            f"to: that directory gives {named}, and a project ID in the inbox is "
            f"{SAFE_FIELD_RULE}. Nothing was changed.",
            kind="refused",
        )
    return project


def resolve_caller(spawn_id: str | None, project_root: Path | None) -> InboxCaller:
    """The caller named by the invocation's ``Caller`` (``get_global_options().caller``),
    which survives ``-C``: *project_root* is the caller's project when ``-C`` named
    another. An ID with no record refuses; it never falls back to a terminal.
    """

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
            )
    raise InboxError(
        f"this command runs as session {spawn_id} (MERIDIAN_SPAWN_ID), which has no record "
        "on this machine, so its role can't be checked. Nothing was changed.",
        kind="refused",
    )


__all__ = ["InboxCaller", "project_of_root", "resolve_caller"]
