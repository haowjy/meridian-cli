"""Map a user reference to what it names: the one place that dispatches on ref kind.

``locate`` answers "which thing is this?" and nothing else. Each command then takes
the projection it needs and never re-classifies the string:

- ``locate_run`` / ``resolve_spawn_target``: the spawn to inspect or act on.
- ``resolve_session_reference`` (``ops/reference.py``): the session to resume.
- ``resolve_transcript_source`` (``ops/session_target.py``): the transcript to read.
- ``resolve_context_ref`` (``ops/spawn/context_ref.py``): the ``--from`` payload.

A native session ID with no record is ``Untracked``: resuming or reading a session
Meridian never saw is a feature, so only those callers accept it. Every other
reference that names nothing is a ``RefNotFound``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from meridian.lib.core.refs import SELECTORS, Ref, RefNotFound, parse_ref
from meridian.lib.core.spawn_lifecycle import ACTIVE_SPAWN_STATUSES
from meridian.lib.core.types import SpawnId
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import session_identity, session_store, spawn_store
from meridian.lib.state.history_changes import HistoryCoordinationError
from meridian.lib.state.history_index import (
    HistoryIndex,
    HistoryIndexIncomplete,
    indexed_spawn_scan,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import overlay_derived


@dataclass(frozen=True)
class Located:
    """What a reference names: a spawn (spawn ref, selector) or a chat (chat ref, native ID)."""

    ref: Ref
    runtime_root: Path
    spawn: SpawnRecord | None = None
    # A chat ref names the chat's newest record; a native ID names the generation
    # recorded under it.
    chat: session_store.SessionRecord | None = None
    # Other chats recorded under the same native ID.
    also_bound: tuple[str, ...] = ()


@dataclass(frozen=True)
class Untracked:
    """A native session ID Meridian has no record of."""

    native_id: str



def _select_latest_spawn_id(
    project_root: Path,
    *,
    statuses: tuple[str, ...] | None,
    runtime_root: Path | None = None,
) -> str | None:
    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return None
    spawns = overlay_derived(
        resolved_runtime_root,
        indexed_spawn_scan(resolved_runtime_root).records,
    )
    if statuses is not None:
        wanted = set(statuses)
        spawns = [item for item in spawns if item.status in wanted]
    if not spawns:
        return None
    return spawns[-1].id


def locate(
    project_root: Path,
    ref: str,
    *,
    runtime_root: Path | None = None,
    harness: str | None = None,
) -> Located | Untracked:
    """Find what ``ref`` names. ``harness`` narrows a native ID that two harnesses share."""

    parsed = parse_ref(ref)
    root = runtime_root or resolve_runtime_root_for_read(project_root)
    if root is None:
        if parsed.kind == "native":
            return Untracked(parsed.value)
        raise RefNotFound(parsed.value, parsed.kind)

    match parsed.kind:
        case "selector":
            spawn_id = _select_latest_spawn_id(
                project_root, statuses=SELECTORS[parsed.value], runtime_root=root
            )
            spawn = spawn_store.get_spawn(root, spawn_id) if spawn_id is not None else None
            if spawn is None:
                raise RefNotFound(parsed.value, "selector")
            return Located(parsed, root, spawn=spawn)
        case "spawn":
            spawn = spawn_store.get_spawn(root, parsed.value)
            if spawn is None:
                raise RefNotFound(parsed.value, "spawn")
            return Located(parsed, root, spawn=spawn)
        case "chat":
            chat = session_store.chat_record(root, parsed.value)
            if chat is None:
                raise RefNotFound(parsed.value, "chat")
            return Located(parsed, root, chat=chat)
        case "native":
            generations = session_store.native_generations(root, parsed.value, harness=harness)
            if not generations:
                return Untracked(parsed.value)
            chat = generations[-1]
            other_chats = dict.fromkeys(
                record.chat_id for record in reversed(generations) if record.chat_id != chat.chat_id
            )
            return Located(parsed, root, chat=chat, also_bound=tuple(other_chats))


def chat_spawn(located: Located) -> SpawnId | None:
    """The spawn that owns the located chat: the one that ran it."""

    assert located.chat is not None
    # Only a chat ref carries the chat's newest record, the fast path for a linked chat.
    return session_identity.chat_owner_spawn(
        located.runtime_root,
        located.chat.chat_id,
        newest=located.chat if located.ref.kind == "chat" else None,
    )


class RunTarget(NamedTuple):
    spawn_id: SpawnId
    # Set when the reference named a chat (or a native session in one).
    chat_id: str | None = None
    # The spawn is live in ``chat_id`` now, rather than merely having run it.
    occupant: bool = False


def locate_run(
    project_root: Path, ref: str, *, runtime_root: Path | None = None
) -> RunTarget:
    """The spawn a command inspects or acts on.

    A spawn ref names that spawn. A chat or native ref names the run live in that chat
    now (it may have taken the chat over with native ``/resume``), else the spawn that
    ran it.
    """

    located = locate(project_root, ref, runtime_root=runtime_root)
    if isinstance(located, Untracked):
        raise RefNotFound(located.native_id, "native")
    if located.spawn is not None:
        return RunTarget(SpawnId(located.spawn.id))
    assert located.chat is not None
    chat_id = located.chat.chat_id
    # Only running rows can hold a chat, so read the index for those, never every
    # spawn's state file. Following a native /resume is best effort: when the index
    # can't answer in time, name the owner rather than fail the command.
    try:
        running = HistoryIndex(located.runtime_root).spawns(status=set(ACTIVE_SPAWN_STATUSES))
    except (HistoryIndexIncomplete, HistoryCoordinationError):
        running = ()
    occupant = session_identity.live_observer_for_chat(
        located.runtime_root, chat_id, records=running
    )
    if occupant is not None:
        return RunTarget(SpawnId(occupant), chat_id, True)
    owner = chat_spawn(located)
    if owner is None:
        raise ValueError(f"Chat {chat_id} has no spawn")
    return RunTarget(owner, chat_id)


def resolve_spawn_target(
    project_root: Path, ref: str, *, runtime_root: Path | None = None
) -> str:
    """The spawn ID a spawn command acts on."""

    return str(locate_run(project_root, ref, runtime_root=runtime_root).spawn_id)


def resolve_spawn_targets(
    project_root: Path, refs: tuple[str, ...], *, runtime_root: Path | None = None
) -> tuple[str, ...]:
    targets = (resolve_spawn_target(project_root, ref, runtime_root=runtime_root) for ref in refs)
    return tuple(dict.fromkeys(targets))


__all__ = [
    "Located",
    "RunTarget",
    "Untracked",
    "chat_spawn",
    "locate",
    "locate_run",
    "resolve_spawn_target",
    "resolve_spawn_targets",
]
