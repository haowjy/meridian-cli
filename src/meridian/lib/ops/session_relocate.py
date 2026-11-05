"""``meridian session relocate``: the one way a chat's recorded paths change.

Relocate owns one rule, ``_Move``: where a recorded path goes. It applies it to the
chat's Meridian records and to the directory the harness records (``native_cwd``), then
hands each harness the finished ``cwd_from -> cwd_to`` change. The read-only questions
about a chat that may have moved live in ``relocate_gate``.
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
from meridian.lib.core.native_identity import NativeKeyFields
from meridian.lib.core.refs import is_chat_id
from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import ChatId
from meridian.lib.core.util import FormatContext
from meridian.lib.harness.adapter import NativeRelocation
from meridian.lib.launch.session_links import session_task_link_path
from meridian.lib.ops.relocate_gate import (
    adapter_for,
    chat_key,
    harness_cwd,
    moved_root,
)
from meridian.lib.ops.runtime import async_from_sync, resolve_roots_for_read
from meridian.lib.platform.atomic import atomic_symlink
from meridian.lib.platform.locking import FileLockTimeout, lock_file
from meridian.lib.state import session_store, spawn_store, work_repository
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.paths import RuntimePaths, rebase_path
from meridian.lib.state.project_identity import is_same_project
from meridian.lib.state.session_fold import (
    PathMove,
    Replacement,
    SessionRecord,
    SessionRelocateEvent,
)
from meridian.lib.state.session_identity import live_observer_for_chat
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import write_state_locked
from meridian.lib.state.spawn_scope import read_spawn_scope, write_spawn_scope_task_dir
from meridian.lib.state.spawn_status import quarantine_may_observe
from meridian.lib.state.work_state import WorkItem
from meridian.lib.state.work_store import list_archived_work_items, list_work_items

_PATH_FIELDS = ("control_root", "task_cwd", "execution_cwd")


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
class _Move:
    """Where a recorded path goes: ``olds`` (the spellings of one old directory) to ``new``.

    ``must_exist`` is the chat form, where the old directory is still a live checkout: a
    path under it moves only when the moved directory exists, so a checkout nested
    inside it (``/r/.wt/a`` when relocating ``/r`` to ``/r/.wt/b``) stays put.
    """

    olds: tuple[str, ...]
    new: str
    must_exist: bool

    def __call__(self, path: str) -> str | None:
        moved = rebase_path(path, self.olds, self.new)
        return None if moved is None or (self.must_exist and not Path(moved).is_dir()) else moved

    def kept(self, path: str) -> bool:
        """``path`` is under the old directory but stays (the chat form's nested checkout)."""
        return rebase_path(path, self.olds, self.new) is not None and self(path) is None


@dataclass(frozen=True)
class _ChatPlan:
    chat_id: str
    harness: str
    fields: tuple[str, ...]  # recorded path fields that move
    keep: tuple[str, ...]  # recorded paths under the old directory that stay
    native: NativeRelocation | None


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


def _moves_any(move: _Move, paths: Iterable[str]) -> bool:
    return any(move(path) is not None for path in paths)


def _old_spellings(old: Path) -> tuple[str, ...]:
    """The typed ``old`` and its canonical form.

    ``old`` no longer exists after a move, so it can't be resolved; its nearest existing
    parent can (`/var` is `/private/var` on macOS), and a record may hold either spelling.
    """
    typed = os.path.abspath(os.path.normpath(old))
    existing = Path(typed)
    tail: list[str] = []
    while not existing.exists() and existing != existing.parent:
        tail.insert(0, existing.name)
        existing = existing.parent
    canonical = str(existing.resolve().joinpath(*tail))
    return tuple(dict.fromkeys((typed, canonical)))


def _load_chats(runtime_root: Path, chat: str | None) -> dict[str, list[SessionRecord]]:
    """Live generations, oldest first, per chat: every chat, or the named one together
    with the chats sharing its native session."""
    if chat is None:
        by_chat: dict[str, list[SessionRecord]] = defaultdict(list)
        for generation in _live(
            session_store.fold_session_generations(
                session_store.chats_rows(runtime_root, None).ordered
            )
        ):
            by_chat[generation.chat_id].append(generation)
        return by_chat
    by_chat = {chat: _live(session_store.chat_generations(runtime_root, chat))}
    if not by_chat[chat]:
        raise ValueError(f"Chat {chat} not found.")
    key = chat_key(by_chat[chat]).complete()
    for other in session_store.native_chats(runtime_root, key.session_id) if key else ():
        if other not in by_chat and (
            rows := _live(session_store.chat_generations(runtime_root, other))
        ):
            by_chat[other] = rows
    return by_chat


def _prefix_targets(
    by_chat: Mapping[str, Sequence[SessionRecord]],
    terminal: Iterable[SpawnRecord],
    pins: Mapping[str, str | None],
    links: Iterable[str],
    move: _Move,
) -> set[str]:
    """Chats with any record under the old directory: the prefix form's selection."""
    targets = {
        chat_id
        for chat_id, generations in by_chat.items()
        if _moves_any(move, _chat_paths(generations))
    }
    targets |= {
        row.entry_chat_id
        for row in terminal
        if row.entry_chat_id and _moves_any(move, _spawn_paths(row))
    }
    targets |= {chat_id for chat_id in pins.values() if chat_id}
    return targets | set(links)


def _with_native_siblings(
    targets: set[str], by_chat: Mapping[str, Sequence[SessionRecord]]
) -> set[str]:
    """A chat sharing a moved chat's native key shares its store and harness record."""
    keys = {key for chat_id in targets if (key := chat_key(by_chat[chat_id]).complete())}
    return targets | {
        chat_id
        for chat_id, generations in by_chat.items()
        if (key := chat_key(generations).complete()) and key in keys
    }


def _plan_native(
    generations: Sequence[SessionRecord], move: _Move, native: Path | None
) -> NativeRelocation | None:
    adapter = adapter_for(generations[-1].harness)
    cwd_from = harness_cwd(generations)
    cwd_to = move(str(cwd_from)) if cwd_from is not None else None
    if adapter is None or cwd_from is None or cwd_to is None:
        return None
    return adapter.plan_native_relocate(
        chat_key(generations), cwd_from=cwd_from, cwd_to=Path(cwd_to), native=native
    )


def _build_plan(
    runtime_root: Path,
    state_dir: Path,
    project_root: Path,
    move: _Move,
    *,
    chat: str | None,
    native: Path | None,
) -> _Plan:
    by_chat = _load_chats(runtime_root, chat)
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
        and move(pin.as_posix()) is not None
    }
    moving_links = {
        chat_id
        for chat_id in by_chat
        if (link := session_task_link_path(runtime_root, chat_id)).is_symlink()
        and move(os.readlink(link)) is not None
    }

    targets = (
        _prefix_targets(by_chat, terminal, moving_pins, moving_links, move)
        if chat is None
        else {chat}
    )
    targets = _with_native_siblings(targets, by_chat)

    native_plans: dict[NativeKeyFields, NativeRelocation | None] = {}
    chats: list[_ChatPlan] = []
    for chat_id in sorted(targets, key=lambda c: int(c[1:]) if is_chat_id(c) else 0):
        generations = by_chat[chat_id]
        identity = chat_key(generations)
        if identity not in native_plans:
            native_plans[identity] = _plan_native(generations, move, native)
        chats.append(
            _ChatPlan(
                chat_id,
                generations[-1].harness,
                tuple(
                    name
                    for name in _PATH_FIELDS
                    if any(
                        (value := getattr(g, name)) and move(value) is not None for g in generations
                    )
                ),
                tuple(dict.fromkeys(p for p in _chat_paths(generations) if move.kept(p))),
                native_plans[identity],
            )
        )
    chat_ids = {c.chat_id for c in chats}
    spawns = tuple(
        row
        for row in terminal
        if row.entry_chat_id in chat_ids and _moves_any(move, _spawn_paths(row))
    )
    pins = tuple(spawn_id for spawn_id, owner in moving_pins.items() if owner in chat_ids)
    links = tuple(sorted(moving_links & chat_ids))
    moving_items: list[WorkItem] = []
    if chat is None:
        for item in (
            *list_work_items(state_dir)[0],
            *list_archived_work_items(state_dir, all_archived=True)[0],
        ):
            if _moves_any(move, work_repository.work_item_paths(item)):
                moving_items.append(item)
    worktrees = any(
        _moves_any(move, [p for p in (item.worktree.path, item.worktree.repo_path) if p])
        for item in moving_items
    )
    return _Plan(
        tuple(c for c in chats if c.fields or c.native is not None),
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
    """Run each harness's plans in one call per harness; the refusal per native key.
    ``plans`` hold no refusal: a refused plan is never applied."""
    by_harness: dict[str, list[NativeRelocation]] = defaultdict(list)
    for plan in dict.fromkeys(plans):
        by_harness[plan.identity.harness or ""].append(plan)
    refusals: dict[NativeKeyFields, str | None] = {}
    for harness, harness_plans in by_harness.items():
        adapter = adapter_for(harness)
        assert adapter is not None  # planned through this adapter
        for result in adapter.apply_native_relocations(harness_plans):
            refusals[result.plan.identity] = result.refusal
    return refusals


def _journal_rows(
    plan: _Plan, move: _Move, refusals: Mapping[NativeKeyFields, str | None]
) -> dict[str, list[SessionRelocateEvent]]:
    """One relocate row per chat that has a path or its store to change."""
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
        paths = (
            PathMove(old=move.olds[0], new=move.new, also=move.olds[1:], keep=chat.keep)
            if chat.fields
            else None
        )
        if paths is not None or store is not None:
            rows[chat.chat_id] = [
                SessionRelocateEvent(
                    chat_id=ChatId(chat.chat_id), relocated_at=now, paths=paths, native_store=store
                )
            ]
    return rows


def _write_other_records(
    runtime_root: Path,
    state_dir: Path,
    project_root: Path,
    move: _Move,
    plan: _Plan,
) -> None:
    """Spawn rows, pins, task links, then work items: each atomic and idempotent on rerun."""
    spawns_dir = RuntimePaths.from_root_dir(runtime_root).spawns_dir

    def move_row(current: SpawnRecord) -> SpawnRecord:
        return current.model_copy(
            update={
                name: move(value) or value
                for name in _PATH_FIELDS
                if (value := getattr(current, name))
            }
        )

    for row in plan.spawns:
        write_state_locked(spawns_dir, row.id, move_row, allow_terminal_overwrite=True)
    for spawn_id in plan.pins:
        pin = read_spawn_scope(project_root, spawn_id).task_dir
        if pin is not None and (moved := move(pin.as_posix())) is not None:
            write_spawn_scope_task_dir(project_root, spawn_id, Path(moved))
    for chat_id in plan.links:
        link = session_task_link_path(runtime_root, chat_id)
        if (moved := move(os.readlink(link))) is not None:
            atomic_symlink(link, Path(moved))
    for work_id in plan.items:
        work_repository.move_work_item_paths(state_dir, work_id, move)


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
        moved_from = moved_root(runtime_root, generations, target=new)
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

    move = _Move(_old_spellings(old), str(new), must_exist=chat is not None)
    native = Path(payload.native).expanduser() if payload.native else None
    plan = _build_plan(runtime_root, state_dir, project_root, move, chat=chat, native=native)
    if native is not None:
        refused = [c.native.refusal for c in plan.chats if c.native and c.native.refusal]
        if refused:
            raise ValueError(f"Refusing --native: {refused[0]}")
        if not any(c.native is not None for c in plan.chats):
            raise ValueError("--native has nothing to apply: the chat's harness directory stays.")

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
        rows = _journal_rows(plan, move, refusals)
        if rows:
            session_store.append_chat_rows(runtime_root, rows)
    _write_other_records(runtime_root, state_dir, project_root, move, plan)
    return output(refusals)


session_relocate = async_from_sync(session_relocate_sync)
