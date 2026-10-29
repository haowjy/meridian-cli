"""Stable per-execution directory links under the runtime root.

Every live execution — a primary or a spawned agent — names two paths that
outlive any one launch, so they never go stale mid-run: ``sessions/<chat>/work``
is its work directory and ``sessions/<chat>/task`` is the checkout it edits.

Each path is two symlinks, one per store. The work store owns
``work-items/<id>/{work,task}`` and moves them with the item; this module owns
``sessions/<chat>/{work,task}``, which point at them (or at the execution's
ambient directory, or at a checkout the execution pinned). The kernel follows both
hops, so ``work done``, ``reopen`` and ``task-dir`` write no session state, and a
chat link depends only on the chat's row and the execution's scope — never on where
the work store keeps an item.

The links belong to an execution's *entry* chat, the one its session scope locks,
so they survive ``--continue`` of that chat, and a command on any chat of the
execution (Claude ``/clear`` or ``/resume`` rotates the conversation) finds that
one chat. A spawn has its own chat and so its own links; they are named in its
``# Meridian run`` user turn rather than the system prompt, so sibling spawns keep
sharing the cached prompt prefix.

Nothing removes a link: it is a rebuildable projection. A live execution's links
follow every move of its item; an execution that has ended keeps the targets its
row named at the end, and the next launch of the chat repoints them. Only removing
the chat or the runtime root removes the link.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import structlog

from meridian.lib.platform.atomic import atomic_symlink
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import RuntimePaths, resolve_ambient_work_dir
from meridian.lib.state.spawn.repository import SpawnStateNewer
from meridian.lib.state.spawn_scope import read_spawn_scope

logger = structlog.get_logger(__name__)

_WORK_LINK = "work"
_TASK_LINK = "task"


class Execution(NamedTuple):
    """One execution: its run ID, the chat that owns its links, every chat whose row
    names its work attachment, and the item it is attached to when that is known."""

    spawn_id: str
    entry_chat: str
    chats: tuple[str, ...]
    work_id: str | None = None


def session_work_link_path(runtime_root: Path, chat_id: str) -> Path:
    """Return the stable work-directory link for one chat (pure path math)."""

    return _link_path(runtime_root, chat_id, _WORK_LINK)


def session_task_link_path(runtime_root: Path, chat_id: str) -> Path:
    """Return the stable source-checkout link for one chat (pure path math)."""

    return _link_path(runtime_root, chat_id, _TASK_LINK)


def _link_path(runtime_root: Path, chat_id: str, name: str) -> Path:
    return RuntimePaths.from_root_dir(runtime_root).sessions_dir / chat_id.strip() / name


def execution_link_chat(
    runtime_root: Path,
    *,
    spawn_id: str,
    chat_id: str | None = None,
) -> str:
    """Return the chat whose links one execution owns.

    A primary passes the chat it was launched on. A spawn passes nothing: its row
    names its own chat, which is what its links and its environment point at. An
    execution with no row yet — a dry run — has no links.
    """

    explicit = (chat_id or "").strip()
    if explicit:
        return explicit
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    return ((row.entry_chat_id or "") if row is not None else "").strip()


def execution_of(runtime_root: Path, *, chat_id: str, spawn_id: str | None) -> Execution:
    """Return the execution a work command on *chat_id* acts for.

    The caller's own spawn row (``MERIDIAN_SPAWN_ID``, one small read) names the
    execution; its entry chat owns the links and its current chat is where the
    conversation is now. A chat that row does not name — an explicit ``--chat`` —
    belongs to the spawn its own record names, if any. A chat of no execution is its
    own: it has rows to write and no links to publish.
    """

    chat = chat_id.strip()
    try:
        row = spawn_store.get_spawn(runtime_root, spawn_id) if spawn_id else None
        if row is None or chat not in (row.entry_chat_id, row.continue_chat_id):
            record = session_store.chat_record(runtime_root, chat)
            named = (record.spawn_id or "").strip() if record is not None else ""
            row = spawn_store.get_spawn(runtime_root, named) if named else None
    except SpawnStateNewer as exc:
        # A newer build's row: this build reads only its chat fields, as the v3 branch does.
        report = exc.report
        entry = (report.chat_id or "").strip() or chat
        return Execution(
            spawn_id=report.spawn_id,
            entry_chat=entry,
            chats=_chats(chat, entry, report.current_chat_id or entry),
        )
    if row is None:
        return Execution(spawn_id="", entry_chat=chat, chats=(chat,))
    entry = (row.entry_chat_id or "").strip() or chat
    return Execution(
        spawn_id=row.id,
        entry_chat=entry,
        chats=_chats(chat, entry, row.continue_chat_id),
    )


def _chats(*chats: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(chat.strip() for chat in chats if chat and chat.strip()))


def _owner_of(runtime_root: Path, record: session_store.SessionRecord) -> Execution:
    """One live session record as an execution.

    A primary's run row, not the chat a command happens to run on, says where the
    execution is now and which chat owns its links: Claude ``/clear`` moves it to a new
    chat while the process keeps running on the old one, whose lease it still holds. The
    new chat inherits the attachment and every later write touches both rows.
    """

    spawn_id = (record.spawn_id or "").strip()
    entry = record.chat_id
    current = record.chat_id
    if record.kind == "primary" and spawn_id:
        row = spawn_store.get_spawn(runtime_root, spawn_id)
        if row is not None:
            entry = (row.entry_chat_id or "").strip() or entry
            current = (row.continue_chat_id or "").strip() or current
    return Execution(
        spawn_id=spawn_id,
        entry_chat=entry,
        chats=_chats(current, entry),
        work_id=record.active_work_id,
    )


def execution_owners(runtime_root: Path) -> tuple[Execution, ...]:
    """Return one execution per live session record.

    One index read and one spawn read per live primary is the whole cost. Reading each
    chat's record separately would re-scan the session journal, and scanning every
    spawn file would grow with the project's history.
    """

    owners: dict[str, Execution] = {}
    for record in session_store.list_active_session_records(runtime_root):
        if not (record.spawn_id or "").strip():
            continue
        owner = _owner_of(runtime_root, record)
        owners.setdefault(owner.entry_chat, owner)
    return tuple(owners.values())


def chat_link_targets(
    runtime_root: Path,
    project_root: Path,
    *,
    work_id: str | None,
    pin: Path | None,
    ambient_dir: Path,
) -> tuple[Path, Path]:
    """Return where one chat's ``(work, task)`` links point.

    Pure: the chat's row names the item (*work_id*) and the execution's scope names
    a checkout it pinned (*pin*); nothing here asks the work store where an item
    lives, because the item's own links answer that. The work link is the item's, or
    the ambient directory with no item. The task link is the pin, else the item's
    checkout, else the project root.
    """

    items = RuntimePaths.from_root_dir(runtime_root).work_items_dir
    work = items / work_id / _WORK_LINK if work_id else ambient_dir
    if pin is not None:
        return work, pin
    return work, items / work_id / _TASK_LINK if work_id else project_root


def publish_chat_links(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    *,
    spawn_id: str,
    work_id: str | None,
) -> tuple[Path, Path]:
    """Point one chat's links at *work_id*, the item its row carries, and its scope's pin.

    Holds the session write epoch, so concurrent ``work switch``es converge on the
    last row written; a caller that just wrote the row passes the value it wrote.
    The item's own links must exist before a chat links to it:
    ``work_repository.ensure_item_links`` runs first, outside this epoch, because the
    work store's lock is never held together with the session locks.
    """

    with session_store.session_writes(runtime_root):
        item = (work_id or "").strip()
        ambient_dir = resolve_ambient_work_dir(project_root, spawn_id, runtime_root=runtime_root)
        if not item:
            # A chat with no item never links to nothing.
            ambient_dir.mkdir(parents=True, exist_ok=True)
        work_target, task_target = chat_link_targets(
            runtime_root,
            project_root,
            work_id=item or None,
            pin=read_spawn_scope(project_root, spawn_id).task_dir,
            ambient_dir=ambient_dir,
        )
        return (
            _publish(runtime_root, chat_id, _WORK_LINK, work_target),
            _publish(runtime_root, chat_id, _TASK_LINK, task_target),
        )


def republish_chat_links(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    *,
    spawn_id: str,
) -> tuple[Path, Path]:
    """Point one chat's links where its row says, for a caller that wrote no row."""

    with session_store.session_writes(runtime_root):
        return publish_chat_links(
            project_root,
            runtime_root,
            chat_id,
            spawn_id=spawn_id,
            work_id=session_store.get_session_active_work_id(runtime_root, chat_id),
        )


def _publish(runtime_root: Path, chat_id: str, name: str, target: Path) -> Path:
    link = _link_path(runtime_root, chat_id, name)
    displaced = atomic_symlink(link, target)
    if displaced is not None:
        logger.warning(
            "Moved a real path out of a session link slot.",
            link=link.as_posix(),
            displaced=displaced.as_posix(),
        )
    return link


__all__ = [
    "Execution",
    "chat_link_targets",
    "execution_link_chat",
    "execution_of",
    "execution_owners",
    "publish_chat_links",
    "republish_chat_links",
    "session_task_link_path",
    "session_work_link_path",
]
