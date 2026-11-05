"""``meridian session relocate``: the one way a chat's recorded paths change.

Two questions about a chat whose directory may have moved, deliberately kept apart:
``needs_relocate`` asks whether its next launch would fail because the harness's own
state points at a moved path; ``moved_root`` asks where it moved from. The gate reads
only harness-native reasons, so a relocate that succeeds always clears it. A missing
Meridian task dir (spawn ``--continue``) is not a gate reason: it fails before launch
on every harness and gets a hint built from ``moved_root`` instead.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.native_identity import NativeKeyFields, NativeStoreMove
from meridian.lib.core.refs import is_chat_id
from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import ChatId, HarnessId
from meridian.lib.core.util import FormatContext
from meridian.lib.harness.adapter import NativeRelocation, SubprocessHarness
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.session_links import session_task_link_path
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.platform.atomic import atomic_symlink
from meridian.lib.platform.locking import FileLockTimeout, lock_file
from meridian.lib.state import session_store, spawn_store, work_repository
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.paths import RuntimePaths, rebase_path
from meridian.lib.state.project_identity import is_same_project
from meridian.lib.state.session_fold import Replacement, SessionRecord, SessionRelocateEvent
from meridian.lib.state.session_identity import live_observer_for_chat
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import write_state_locked
from meridian.lib.state.spawn_scope import read_spawn_scope, write_spawn_scope_task_dir
from meridian.lib.state.spawn_status import quarantine_may_observe
from meridian.lib.state.work_state import WorkItem
from meridian.lib.state.work_store import list_archived_work_items, list_work_items


@dataclass(frozen=True)
class RelocateNeed:
    """Why the next launch of a chat would fail: its store moved, or the directory the
    harness records for it is gone."""

    store_move: NativeStoreMove | None = None
    missing_dir: Path | None = None


def needs_relocate(
    generations: Sequence[SessionRecord],
    adapter: SubprocessHarness,
    *,
    control_root: Path,
    child_env: Mapping[str, str],
) -> RelocateNeed | None:
    """The gate: would resuming this chat from ``control_root`` fail on a moved path?

    ``generations`` are the chat's (``session_store.chat_generations``). The store check
    agrees with the launch's finalize check only when ``child_env`` is that launch's
    final child env.
    """
    newest_first = tuple(reversed(generations))
    identity = _chat_key(newest_first)
    if identity.native_store is not None:
        attempted = adapter.resume_store(
            identity.native_store, child_env=child_env, child_cwd=control_root
        )
        kind = (
            adapter.classify_store_move(recorded=identity.native_store, attempted=attempted)
            if attempted != identity.native_store
            else None
        )
        if kind is not None:
            return RelocateNeed(store_move=NativeStoreMove(identity.native_store, attempted, kind))
    resumes_in = adapter.native_cwd(identity, recorded_roots=_control_roots(newest_first))
    if resumes_in is not None and not resumes_in.is_dir():
        return RelocateNeed(missing_dir=resumes_in)
    return None


def refused_relocation(
    runtime_root: Path,
    generations: Sequence[SessionRecord],
    adapter: SubprocessHarness,
    *,
    target: Path,
) -> str | None:
    """The gate's other state: the chat's newest relocate already moved it to ``target``,
    yet the harness still records that relocate's old directory.

    Relocate writes the chat's Meridian paths even when the harness refuses its own
    change, so callers show this instead of offering relocate again, which would loop.
    None when no such relocate exists or the harness no longer records its old place.
    """
    chat_id = generations[-1].chat_id
    moves = [
        row.paths
        for row in session_store.chat_rows(runtime_root, chat_id)
        if isinstance(row, SessionRelocateEvent) and row.paths is not None
    ]
    if not moves or not target.is_relative_to(moves[-1].new):
        return None
    newest_first = tuple(reversed(generations))
    old, new = Path(moves[-1].old), Path(moves[-1].new)
    plan = adapter.plan_native_relocate(
        _chat_key(newest_first),
        old_roots=(old,),
        new_root=new,
        recorded_roots=_control_roots(newest_first),
    )
    if plan is None:
        return None
    reason = plan.refusal or f"rerun `meridian session relocate {chat_id}` to finish"
    return f"{chat_id} was relocated to {new}, but {adapter.id} still records {old}: {reason}"


def moved_root(
    generations: Sequence[SessionRecord], adapter: SubprocessHarness, *, target: Path
) -> Path | None:
    """The directory this chat moved from to ``target``: relocate's default ``--from``.

    The harness's own record wins, then each generation newest first, by ``task_cwd``,
    ``execution_cwd``, ``control_root``. Any recorded path equal to ``target`` is
    skipped: a ``--continue`` from the new directory, by a build without the gate,
    appended a start that records it.
    """
    newest_first = tuple(reversed(generations))
    roots = tuple(root for root in _control_roots(newest_first) if root != target)
    recorded = adapter.native_cwd(_chat_key(newest_first), recorded_roots=roots)
    if recorded is not None and recorded != target:
        return recorded
    for generation in newest_first:
        # Legacy rows carry no control_root.
        for field in (generation.task_cwd, generation.execution_cwd, generation.control_root):
            if field and Path(field) != target:
                return Path(field)
    return None


def _chat_key(newest_first: Sequence[SessionRecord]) -> NativeKeyFields:
    for generation in newest_first:
        if generation.native_key() is not None:
            return generation.key_fields()
    return newest_first[0].key_fields() if newest_first else NativeKeyFields()


def _control_roots(newest_first: Sequence[SessionRecord]) -> tuple[Path, ...]:
    return tuple(Path(g.control_root) for g in newest_first if g.control_root)


# --- the command -----------------------------------------------------------------------


class SessionRelocateInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str | None = None
    from_root: str | None = None
    to_root: str | None = None
    native: str | None = None
    dry_run: bool = False
    project_root: str | None = None


class ChatChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: str
    harness: str
    fields: tuple[str, ...] = ()
    harness_change: str | None = None
    refusal: str | None = None


class SessionRelocateOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    dry_run: bool = False
    old: str = ""
    new: str = ""
    chats: tuple[ChatChange, ...] = ()
    spawns: tuple[str, ...] = ()
    pins: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    work_items: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    undo: str | None = None
    already_at: str | None = None  # where a bare `relocate cN` finds the chat already

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not (self.chats or self.spawns or self.pins or self.links or self.work_items):
            if self.already_at:
                return (
                    f"Nothing to relocate: the chat's records already point at {self.already_at}."
                )
            return f"Nothing to relocate: no record still names {self.old}."
        lines = [
            f"{'Plan (nothing changed)' if self.dry_run else 'Relocated'}: {self.old} -> {self.new}"
        ]
        for chat in self.chats:
            what = ", ".join(chat.fields) or "paths already current"
            lines.append(f"  {chat.chat_id} ({chat.harness}): {what}")
            if chat.harness_change:
                lines.append(f"    {chat.harness}: {chat.harness_change}")
            if chat.refusal:
                lines.append(f"    {chat.harness} change refused: {chat.refusal}")
        if self.spawns:
            lines.append(f"  spawn rows: {', '.join(self.spawns)}")
        if self.pins:
            lines.append(f"  spawn task_dir pins: {', '.join(self.pins)}")
        if self.links:
            lines.append(f"  task links: {', '.join(self.links)}")
        if self.work_items:
            lines.append(f"  work items: {', '.join(self.work_items)}")
        lines.extend(self.notes)
        if self.undo:
            lines.append(f"Undo: {self.undo}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _ChatPlan:
    chat_id: str
    harness: str
    pairs: tuple[tuple[str, str], ...]  # path prefix pairs that match this chat
    native: NativeRelocation | None

    @property
    def fields(self) -> tuple[str, ...]:
        return ("control_root", "task_cwd", "execution_cwd") if self.pairs else ()


@dataclass(frozen=True)
class _Plan:
    chats: tuple[_ChatPlan, ...]
    spawns: tuple[SpawnRecord, ...]  # terminal rows whose paths move
    pins: tuple[str, ...]  # spawn ids whose scope.json task_dir moves
    links: tuple[str, ...]  # chats whose task link moves
    items: tuple[str, ...]  # work items whose paths move
    worktrees: bool  # some item's worktree path moves, which git's own metadata still holds


def _live(generations: Iterable[SessionRecord]) -> list[SessionRecord]:
    return [generation for generation in generations if generation.record_mode == "live"]


def _chat_paths(generations: Iterable[SessionRecord]) -> tuple[str, ...]:
    return tuple(
        path
        for generation in generations
        for path in (generation.control_root, generation.task_cwd, generation.execution_cwd)
        if path
    )


def _spawn_paths(row: SpawnRecord) -> tuple[str, ...]:
    return tuple(path for path in (row.control_root, row.task_cwd, row.execution_cwd) if path)


def _adapter(harness: str) -> SubprocessHarness | None:
    try:
        return get_default_harness_registry().get_subprocess_harness(HarnessId(harness))
    except KeyError:
        return None  # a record naming a harness this build has no adapter for


def _matching_pairs(
    pairs: Sequence[tuple[str, str]], paths: Iterable[str]
) -> tuple[tuple[str, str], ...]:
    held = tuple(paths)
    return tuple(
        pair for pair in pairs if any(rebase_path(path, (pair[0],), pair[1]) for path in held)
    )


def _rebase(pairs: Sequence[tuple[str, str]], path: str) -> str | None:
    return rebase_path(path, [old for old, _ in pairs], pairs[0][1])


def _prefix_pairs(old: Path, new: Path) -> tuple[tuple[str, str], ...]:
    """The typed ``old`` and its canonical form, both to the resolved ``new``.

    ``old`` no longer exists, so it can't be resolved; its nearest existing parent can
    (`/var` is `/private/var` on macOS), and a record may hold either spelling.
    """
    typed = os.path.abspath(os.path.normpath(old))
    existing = Path(typed)
    tail: list[str] = []
    while not existing.exists() and existing != existing.parent:
        tail.insert(0, existing.name)
        existing = existing.parent
    canonical = str(existing.resolve().joinpath(*tail))
    target = str(new)
    return tuple(dict.fromkeys(((typed, target), (canonical, target))))


def _build_plan(
    runtime_root: Path,
    state_dir: Path,
    project_root: Path,
    pairs: Sequence[tuple[str, str]],
    new: Path,
    *,
    chat: str | None,
    native: Path | None,
) -> _Plan:
    if chat is None:
        by_chat: dict[str, list[SessionRecord]] = defaultdict(list)
        for generation in _live(
            session_store.fold_session_generations(
                session_store.chats_rows(runtime_root, None).ordered
            )
        ):
            by_chat[generation.chat_id].append(generation)
    else:
        by_chat = {chat: _live(session_store.chat_generations(runtime_root, chat))}
        if not by_chat[chat]:
            raise ValueError(f"Chat {chat} not found.")
        key = _chat_key(tuple(reversed(by_chat[chat]))).complete()
        for other in session_store.native_chats(runtime_root, key.session_id) if key else ():
            if other not in by_chat and (
                rows := _live(session_store.chat_generations(runtime_root, other))
            ):
                by_chat[other] = rows

    scan = spawn_store.list_spawns(runtime_root)
    terminal = tuple(
        row
        for row in scan.records
        if row.status in TERMINAL_SPAWN_STATUSES
        and row.record_mode == "live"
        and row.entry_chat_id in by_chat
    )

    # Pins and task links name a path too: a crash after the spawn rows leaves them as
    # the only record a rerun still finds.
    moving_pins = {
        row.id: row.entry_chat_id
        for row in terminal
        if (pin := read_spawn_scope(project_root, row.id).task_dir) is not None
        and _rebase(pairs, pin.as_posix()) is not None
    }
    moving_links = {
        chat_id
        for chat_id in by_chat
        if (link := session_task_link_path(runtime_root, chat_id)).is_symlink()
        and _rebase(pairs, os.readlink(link)) is not None
    }

    if chat is None:
        targets = {
            chat_id
            for chat_id, generations in by_chat.items()
            if _matching_pairs(pairs, _chat_paths(generations))
        }
        targets |= {
            row.entry_chat_id
            for row in terminal
            if row.entry_chat_id and _matching_pairs(pairs, _spawn_paths(row))
        }
        targets |= {chat_id for chat_id in moving_pins.values() if chat_id}
        targets |= moving_links
        # A chat sharing a moved chat's native key shares its store and harness record.
        keys = {
            key
            for chat_id in targets
            if (key := _chat_key(tuple(reversed(by_chat[chat_id]))).complete())
        }
        targets |= {
            chat_id
            for chat_id, generations in by_chat.items()
            if (key := _chat_key(tuple(reversed(generations))).complete()) and key in keys
        }
    else:
        key = _chat_key(tuple(reversed(by_chat[chat]))).complete()
        targets = {
            chat_id
            for chat_id, generations in by_chat.items()
            if chat_id == chat
            or (key is not None and _chat_key(tuple(reversed(generations))).complete() == key)
        }

    native_plans: dict[NativeKeyFields, NativeRelocation | None] = {}
    chats: list[_ChatPlan] = []
    for chat_id in sorted(targets, key=lambda c: int(c[1:]) if is_chat_id(c) else 0):
        generations = by_chat[chat_id]
        newest_first = tuple(reversed(generations))
        identity = _chat_key(newest_first)
        harness = generations[-1].harness
        adapter = _adapter(harness)
        if identity not in native_plans:
            native_plans[identity] = (
                adapter.plan_native_relocate(
                    identity,
                    old_roots=tuple(Path(old_spelling) for old_spelling, _ in pairs),
                    new_root=new,
                    native=native,
                    recorded_roots=_control_roots(newest_first),
                )
                if adapter is not None
                else None
            )
        chats.append(
            _ChatPlan(
                chat_id,
                harness,
                _matching_pairs(pairs, _chat_paths(generations)),
                native_plans[identity],
            )
        )
    chat_ids = {c.chat_id for c in chats}
    spawns = tuple(
        row
        for row in terminal
        if row.entry_chat_id in chat_ids and _matching_pairs(pairs, _spawn_paths(row))
    )
    pins = tuple(spawn_id for spawn_id, owner in moving_pins.items() if owner in chat_ids)
    links = tuple(sorted(moving_links & chat_ids))
    moving_items: list[WorkItem] = []
    if chat is None:
        for item in (
            *list_work_items(state_dir)[0],
            *list_archived_work_items(state_dir, all_archived=True)[0],
        ):
            if _matching_pairs(pairs, work_repository.work_item_paths(item)):
                moving_items.append(item)
    worktrees = any(
        _matching_pairs(pairs, [p for p in (item.worktree.path, item.worktree.repo_path) if p])
        for item in moving_items
    )
    return _Plan(
        tuple(c for c in chats if c.pairs or c.native is not None),
        spawns,
        pins,
        links,
        tuple(item.name for item in moving_items),
        worktrees,
    )


def _running_observers(runtime_root: Path, chat_ids: Iterable[str]) -> list[str]:
    """Chats a run holds, by lock or by native ``/resume``; listed once, spawns read once."""
    scan = spawn_store.list_spawns(runtime_root)
    busy: list[str] = []
    for chat_id in chat_ids:
        if any(
            quarantine_may_observe(runtime_root, report, chat_id) for report in scan.quarantines
        ):
            busy.append(f"{chat_id} (a spawn row this build cannot read may be running it)")
        elif (
            live := live_observer_for_chat(runtime_root, chat_id, records=scan.records)
        ) is not None:
            busy.append(f"{chat_id} (live run {live})")
    return busy


def _apply_harness_changes(plans: Iterable[NativeRelocation]) -> dict[NativeKeyFields, str | None]:
    """Run each harness's plans in one call per harness; the refusal per native key."""
    by_harness: dict[str, list[NativeRelocation]] = defaultdict(list)
    for plan in dict.fromkeys(plans):
        by_harness[plan.identity.harness or ""].append(plan)
    refusals: dict[NativeKeyFields, str | None] = {}
    for harness, harness_plans in by_harness.items():
        adapter = _adapter(harness)
        results = adapter.apply_native_relocations(harness_plans) if adapter is not None else ()
        for result in results:
            refusals[result.plan.identity] = result.refusal
    return refusals


def _journal_rows(
    plan: _Plan, refusals: Mapping[NativeKeyFields, str | None]
) -> dict[str, list[SessionRelocateEvent]]:
    now = utc_now_iso()
    rows: dict[str, list[SessionRelocateEvent]] = {}
    for chat in plan.chats:
        store = None
        if (
            chat.native is not None
            and chat.native.native_store is not None
            and chat.native.identity.native_store is not None
            and refusals.get(chat.native.identity) is None
        ):
            store = Replacement(old=chat.native.identity.native_store, new=chat.native.native_store)
        events = [
            SessionRelocateEvent(
                chat_id=ChatId(chat.chat_id),
                relocated_at=now,
                paths=Replacement(old=old, new=new),
                native_store=store if index == 0 else None,
            )
            for index, (old, new) in enumerate(chat.pairs)
        ]
        if store is not None and not events:
            events = [
                SessionRelocateEvent(
                    chat_id=ChatId(chat.chat_id), relocated_at=now, native_store=store
                )
            ]
        if events:
            rows[chat.chat_id] = events
    return rows


def _write_other_records(
    runtime_root: Path,
    state_dir: Path,
    project_root: Path,
    pairs: Sequence[tuple[str, str]],
    plan: _Plan,
) -> None:
    """Spawn rows, pins, task links, then work items: each atomic and idempotent on rerun."""
    spawns_dir = RuntimePaths.from_root_dir(runtime_root).spawns_dir

    def move_row(current: SpawnRecord) -> SpawnRecord:
        return current.model_copy(
            update={
                name: _rebase(pairs, value) or value
                for name in ("control_root", "task_cwd", "execution_cwd")
                if (value := getattr(current, name))
            }
        )

    for row in plan.spawns:
        write_state_locked(spawns_dir, row.id, move_row, allow_terminal_overwrite=True)
    for spawn_id in plan.pins:
        pin = read_spawn_scope(project_root, spawn_id).task_dir
        if pin is not None and (moved := _rebase(pairs, pin.as_posix())) is not None:
            write_spawn_scope_task_dir(project_root, spawn_id, Path(moved))
    for chat_id in plan.links:
        link = session_task_link_path(runtime_root, chat_id)
        if (moved := _rebase(pairs, os.readlink(link))) is not None:
            atomic_symlink(link, Path(moved))
    for work_id in plan.items:
        work_repository.move_work_item_paths(state_dir, work_id, lambda path: _rebase(pairs, path))


def session_relocate_sync(payload: SessionRelocateInput) -> SessionRelocateOutput:
    chat = payload.ref.strip() if payload.ref else None
    if chat is not None and not is_chat_id(chat):
        raise ValueError(f"'{chat}' is not a chat id (c123).")
    if chat is None and (payload.from_root is None or payload.to_root is None):
        raise ValueError("Name a chat (`relocate c123`), or give both --from and --to.")
    if chat is None and payload.native:
        raise ValueError("--native picks one chat's transcript; give a chat id.")

    resolution = resolve_project_root_resolution(
        Path(payload.project_root).expanduser().resolve() if payload.project_root else None
    )
    project_root = resolution.project_root
    roots = resolve_roots_for_read(str(project_root))
    if roots is None:
        raise ValueError(f"No Meridian state for {project_root}: nothing to relocate.")
    runtime_root, state_dir = roots.runtime_root, roots.project_state_dir

    new = Path(payload.to_root).expanduser() if payload.to_root else project_root
    if not new.is_dir():
        raise ValueError(f"--to {new} is not an existing directory.")
    new = new.resolve()
    if not is_same_project(new, project_root):
        raise ValueError(f"--to {new} belongs to a different project than {project_root}.")

    if payload.from_root:
        old = Path(os.path.abspath(os.path.expanduser(payload.from_root)))
    else:
        assert chat is not None
        generations = session_store.chat_generations(runtime_root, chat)
        if not generations:
            raise ValueError(f"Chat {chat} not found.")
        adapter = _adapter(generations[-1].harness)
        moved_from = moved_root(generations, adapter, target=new) if adapter else None
        if moved_from is None:
            raise ValueError(f"Cannot tell where {chat} moved from; pass --from.")
        old = moved_from
    if old == new:
        raise ValueError(f"--from and --to are both {new}.")
    if chat is None and old.exists():
        raise ValueError(
            f"{old} still exists, so the chats under it belong to a live checkout. "
            "Relocate one chat with `meridian session relocate c123` instead."
        )

    pairs = _prefix_pairs(old, new)
    native = Path(payload.native).expanduser() if payload.native else None
    plan = _build_plan(runtime_root, state_dir, project_root, pairs, new, chat=chat, native=native)
    if native is not None:
        if any(c.harness != "claude" for c in plan.chats) or not any(
            c.native is not None for c in plan.chats
        ):
            raise ValueError("--native is only supported for Claude chats.")
        refused = [c.native.refusal for c in plan.chats if c.native and c.native.refusal]
        if refused:
            raise ValueError(f"Refusing --native: {refused[0]}")

    prefix = f"meridian session relocate {chat} " if chat else "meridian session relocate "
    rerun = f"{prefix}--from {shlex.quote(str(old))} --to {shlex.quote(str(new))}"
    reverse = f"{prefix}--from {shlex.quote(str(new))} --to {shlex.quote(str(old))}"
    undo = (
        reverse if chat and old.is_dir() else f"move {shlex.quote(str(old))} back, then {reverse}"
    )

    already_at = None
    if chat is not None and payload.from_root is None:
        # Bare form: `old` is where the harness's own record says the chat was, which
        # after a full relocate is the stale original. Say where Meridian's records are.
        newest = session_store.chat_generations(runtime_root, chat)[-1]
        here = next((p for p in (newest.task_cwd, newest.control_root) if p), None)
        already_at = here if here and Path(here).is_relative_to(new) else None

    def output(refusals: Mapping[NativeKeyFields, str | None]) -> SessionRelocateOutput:
        return SessionRelocateOutput(
            dry_run=payload.dry_run,
            old=str(old),
            new=str(new),
            chats=tuple(
                ChatChange(
                    chat_id=c.chat_id,
                    harness=c.harness,
                    fields=c.fields,
                    harness_change=c.native.change if c.native and not c.native.refusal else None,
                    refusal=(c.native.refusal or refusals.get(c.native.identity))
                    if c.native
                    else None,
                )
                for c in plan.chats
            ),
            spawns=tuple(row.id for row in plan.spawns),
            pins=plan.pins,
            links=plan.links,
            work_items=plan.items,
            notes=(
                ("Git keeps its own worktree paths: run `git worktree repair`.",)
                if plan.worktrees
                else ()
            ),
            undo=undo,
            already_at=already_at,
        )

    if payload.dry_run or not (plan.chats or plan.spawns or plan.pins or plan.links or plan.items):
        return output({})

    paths = RuntimePaths.from_root_dir(runtime_root)
    with ExitStack() as stack:
        stack.enter_context(
            lock_file(paths.project_lifetime_flock, mode="shared", read_only_existing=True)
        )
        busy: list[str] = []
        for chat_plan in plan.chats:
            try:
                stack.enter_context(
                    lock_file(paths.sessions_dir / f"{chat_plan.chat_id}.lock", timeout=0)
                )
            except FileLockTimeout:
                busy.append(f"{chat_plan.chat_id} (locked by a run)")
        busy += _running_observers(runtime_root, [c.chat_id for c in plan.chats])
        if busy:
            raise ValueError("Refusing to relocate chats that are running: " + ", ".join(busy))
        print(f"Relocating; if interrupted, rerun: {rerun}", file=sys.stderr)
        refusals = _apply_harness_changes(
            c.native for c in plan.chats if c.native and not c.native.refusal
        )
        rows = _journal_rows(plan, refusals)
        if rows:
            session_store.append_chat_rows(runtime_root, rows)
    _write_other_records(runtime_root, state_dir, project_root, pairs, plan)
    return output(refusals)


session_relocate = async_from_sync(session_relocate_sync)
