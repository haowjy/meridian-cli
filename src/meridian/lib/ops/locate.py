"""Map a user reference to what it names: the one place that dispatches on ref kind.

``locate`` answers "which thing is this?" and nothing else. Each command then takes
the projection it needs and never re-classifies the string:

- ``locate_run`` / ``resolve_spawn_target``: the spawn to inspect or act on.
- ``resolve_session_reference`` (``ops/reference.py``): the session to resume.
- ``resolve_transcript_source`` (``ops/session_target.py``): the transcript to read.
- ``resolve_context_ref`` (``ops/spawn/context_ref.py``): the ``--from`` payload.

A spawn or chat ID asks ``claim_root`` first, so a claimed ID finds its project from any
directory; a legacy ID (no claim) and a selector or native ID look in the current
project. A hit carries the runtime root and project root it was found in.

A native session ID with no record is ``Untracked``: reading a session Meridian never
saw, and primary ``--continue`` / ``--fork`` and ``spawn --fork``, accept it. ``spawn
--continue`` refuses it (it needs retained spawn metadata). Every other reference that
names nothing is a ``RefNotFound``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

from meridian.lib.core.caller import same_runtime_root
from meridian.lib.core.refs import (
    INBOX_IS_NO_SESSION,
    SELECTORS,
    Ref,
    RefAmbiguous,
    RefNotFound,
    is_spawn_id,
    parse_ref,
    project_is_named,
)
from meridian.lib.core.spawn_lifecycle import ACTIVE_SPAWN_STATUSES, is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.machines.resolve import MachineError, resolve_machine
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import session_identity, session_store, spawn_store
from meridian.lib.state.history_changes import HistoryCoordinationError
from meridian.lib.state.history_index import (
    HistoryIndex,
    HistoryIndexIncomplete,
    indexed_spawn_scan,
)
from meridian.lib.state.machine_ids import claim_root, lowest_claim
from meridian.lib.state.saved_machines import SavedMachine
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import overlay_derived, read_derived


@dataclass(frozen=True)
class SpawnHit:
    """A spawn ref or selector: the spawn it names.

    ``project_root`` is where the project lives: the invoking project's for a hit in
    its own runtime root, else the row's ``control_root``, which may be ``None`` or a
    directory that no longer exists.
    """

    ref: Ref
    runtime_root: Path
    spawn: SpawnRecord
    project_root: Path | None = None


@dataclass(frozen=True)
class ChatHit:
    """A chat ref or a native ID that has records: the chat it names."""

    ref: Ref
    runtime_root: Path
    # A chat ref names the chat's newest record; a native ID names the generation
    # ``native_generations`` picks: newest with a native session key, else newest.
    chat: session_store.SessionRecord
    # Other chats recorded under the same native ID.
    also_bound: tuple[str, ...] = ()
    # As ``SpawnHit.project_root``; for another root, from the spawn that owns the chat.
    project_root: Path | None = None


Located = SpawnHit | ChatHit


@dataclass(frozen=True)
class Untracked:
    """A native session ID Meridian has no record of."""

    native_id: str


def _select_latest_spawn_id(runtime_root: Path, statuses: tuple[str, ...] | None) -> str | None:
    spawns = overlay_derived(runtime_root, indexed_spawn_scan(runtime_root).records)
    if statuses is not None:
        wanted = set(statuses)
        spawns = [item for item in spawns if item.status in wanted]
    return spawns[-1].id if spawns else None


def _read_id(ref: Ref, root: Path) -> SpawnRecord | session_store.SessionRecord | None:
    if ref.kind == "spawn":
        return spawn_store.get_spawn(root, ref.value)
    return session_store.chat_record(root, ref.value)


def require_project_dir(project_root: Path | None, ref: str) -> Path:
    """The directory of the project a located session belongs to, for a command that
    must work in it. A row records where it was launched; the directory may be gone."""

    if project_root is None or not project_root.is_dir():
        where = f"'{project_root}', which no longer exists" if project_root else "unknown"
        raise ValueError(
            f"'{ref}' belongs to a project whose directory is {where}, "
            "so this command can't act on it from here."
        )
    return project_root


def _owner_project_root(hit: SpawnHit | ChatHit) -> Path | None:
    """The project directory the row of ``hit`` was launched from."""

    if isinstance(hit, SpawnHit):
        row: SpawnRecord | None = hit.spawn
    else:
        owner = chat_spawn(hit)
        row = spawn_store.get_spawn(hit.runtime_root, owner) if owner is not None else None
    return Path(row.control_root) if row is not None and row.control_root else None


def _hit(ref: Ref, root: Path, found: SpawnRecord | session_store.SessionRecord) -> Located:
    if isinstance(found, SpawnRecord):
        return SpawnHit(ref, root, found)
    return ChatHit(ref, root, found)


def _local_ref(parsed: Ref) -> Ref:
    """``<machine>/<id>`` for this machine is the bare ID, whichever command it reaches; for
    another machine it is an error here. A command that crosses has forwarded such a reference
    before it gets this far, so what arrives is a command that may not."""

    if parsed.kind == "inbox":
        raise ValueError(INBOX_IS_NO_SESSION)
    if parsed.kind != "machine":
        return parsed
    assert parsed.machine is not None
    try:
        target = resolve_machine(parsed.machine)
    except MachineError as exc:
        raise ValueError(exc.text) from exc
    if isinstance(target, SavedMachine):
        raise ValueError(
            f"`{parsed.machine}/{parsed.value}` names another machine, and this command "
            "doesn't reach another machine"
        )
    return Ref("spawn" if is_spawn_id(parsed.value) else "chat", parsed.value)


def locate(
    project_root: Path,
    ref: str,
    *,
    runtime_root: Path | None = None,
    harness: str | None = None,
) -> Located | Untracked:
    """Find what ``ref`` names. ``harness`` narrows a native ID that two harnesses share.

    Raises ``RefAmbiguous`` when a claimed ID also names an unclaimed row in the current
    project and the user did not name a project with ``-C``.
    """

    parsed = _local_ref(parse_ref(ref))
    own = runtime_root or resolve_runtime_root_for_read(project_root)
    claimed = claim_root(parsed.value) if parsed.kind in ("spawn", "chat") else None
    if claimed is not None and (own is None or not same_runtime_root(claimed, own)):
        found = _read_id(parsed, claimed)
        here = _read_id(parsed, own) if own is not None else None
        if own is not None and here is not None and (found is None or project_is_named()):
            return replace(_hit(parsed, own, here), project_root=project_root)
        if found is None:
            raise RefNotFound(parsed.value, parsed.kind, claimed_root=claimed)
        hit = _hit(parsed, claimed, found)
        hit = replace(hit, project_root=_owner_project_root(hit))
        if here is not None:
            raise RefAmbiguous(parsed.value, str(project_root), str(hit.project_root or claimed))
        return hit
    if own is None:
        if parsed.kind == "native":
            return Untracked(parsed.value)
        raise RefNotFound(parsed.value, parsed.kind, legacy=_is_legacy(parsed, claimed))
    located = _locate_in(parsed, own, harness=harness, claimed=claimed)
    if isinstance(located, Untracked):
        return located
    return replace(located, project_root=project_root)


def _is_legacy(ref: Ref, claimed: Path | None) -> bool:
    """An ID with no claim was issued before claims only when its number is below the
    lowest claim of its kind; above it, it was never issued."""

    if ref.kind not in ("spawn", "chat") or claimed is not None:
        return False
    lowest = lowest_claim("p" if ref.kind == "spawn" else "c")
    return lowest is None or int(ref.value[1:]) < lowest


def _locate_in(
    parsed: Ref, root: Path, *, harness: str | None, claimed: Path | None
) -> Located | Untracked:
    match parsed.kind:
        case "selector":
            spawn_id = _select_latest_spawn_id(root, SELECTORS[parsed.value])
            spawn = spawn_store.get_spawn(root, spawn_id) if spawn_id is not None else None
            if spawn is None:
                raise RefNotFound(parsed.value, "selector")
            return SpawnHit(parsed, root, spawn)
        case "spawn" | "chat":
            found = _read_id(parsed, root)
            if found is None:
                raise RefNotFound(parsed.value, parsed.kind, legacy=_is_legacy(parsed, claimed))
            return _hit(parsed, root, found)
        case "native":
            generations = session_store.native_generations(root, parsed.value, harness=harness)
            if not generations:
                return Untracked(parsed.value)
            chat = generations[-1]
            other_chats = dict.fromkeys(
                record.chat_id for record in reversed(generations) if record.chat_id != chat.chat_id
            )
            return ChatHit(parsed, root, chat, tuple(other_chats))
        case "machine" | "inbox":
            raise AssertionError("a machine reference is made local before it is located")


def chat_spawn(hit: ChatHit) -> SpawnId | None:

    # Only a chat ref carries the chat's newest record, the fast path for a linked chat.
    return session_identity.chat_owner_spawn(
        hit.runtime_root,
        hit.chat.chat_id,
        newest=hit.chat if hit.ref.kind == "chat" else None,
    )


class RunTarget(NamedTuple):
    """The spawn a command acts on, with where it lives: a bare spawn ID is not enough once
    IDs reach other projects. ``project_root`` is as ``SpawnHit.project_root``."""

    spawn_id: SpawnId
    runtime_root: Path
    project_root: Path | None
    # Set when the reference named a chat (or a native session in one).
    chat_id: str | None = None
    # The spawn is live in ``chat_id`` now, rather than merely having run it.
    occupant: bool = False


def locate_run(project_root: Path, ref: str, *, runtime_root: Path | None = None) -> RunTarget:
    """The spawn a command inspects or acts on.

    A spawn ref names that spawn. A chat or native ref names the run live in that chat
    now (it may have taken the chat over with native ``/resume``), else the spawn that
    ran it.
    """

    located = locate(project_root, ref, runtime_root=runtime_root)
    if isinstance(located, Untracked):
        raise RefNotFound(located.native_id, "native")
    if isinstance(located, SpawnHit):
        return RunTarget(SpawnId(located.spawn.id), located.runtime_root, located.project_root)
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
        return RunTarget(
            SpawnId(occupant), located.runtime_root, located.project_root, chat_id, True
        )
    owner = chat_spawn(located)
    if owner is None:
        raise ValueError(f"Chat {chat_id} has no spawn")
    return RunTarget(owner, located.runtime_root, located.project_root, chat_id)


def resolve_spawn_target(
    project_root: Path, ref: str, *, runtime_root: Path | None = None
) -> RunTarget:
    return locate_run(project_root, ref, runtime_root=runtime_root)


def resolve_spawn_targets(
    project_root: Path, refs: tuple[str, ...], *, runtime_root: Path | None = None
) -> tuple[RunTarget, ...]:
    """Each reference's target once, in order; targets may sit in different projects."""

    targets = (resolve_spawn_target(project_root, ref, runtime_root=runtime_root) for ref in refs)
    return tuple({(target.runtime_root, target.spawn_id): target for target in targets}.values())


def run_is_live(target: RunTarget) -> bool | None:
    """Whether the run a reference names is running, and for a chat reference still in that
    chat; ``None`` when its record is gone. A chat ID names the run live in it, so a run that
    left the chat (a native ``/resume`` elsewhere) is not that chat's run any more."""

    record = read_derived(target.runtime_root, target.spawn_id)
    if record is None:
        return None
    return is_active_spawn_status(record.status) and (
        target.chat_id is None
        or target.occupant
        or record.current_chat_id in (None, target.chat_id)
    )


__all__ = [
    "ChatHit",
    "Located",
    "RunTarget",
    "SpawnHit",
    "Untracked",
    "chat_spawn",
    "locate",
    "locate_run",
    "require_project_dir",
    "resolve_spawn_target",
    "resolve_spawn_targets",
    "run_is_live",
]
