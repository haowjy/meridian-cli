"""Stable per-chat directory links under the runtime root.

A primary names two paths that outlive any one launch, so they never go stale
mid-conversation: ``sessions/<chat>/work`` is its work directory and
``sessions/<chat>/task`` is the checkout it edits. Both are projections of
durable state, never authority — the session row, the work item and the spawn
scope decide the targets, and every primary launch and every command that moves
those targets repoints them.

The links belong to a primary execution's *entry* chat, the one its session scope
locks, so they survive ``--continue`` of that chat, and a command on any chat of
the execution (Claude ``/clear`` or ``/resume`` rotates the conversation) finds
that one chat. A spawn keeps concrete launch-bound directories: a child inherits
its parent's chat id, so a chat-keyed link would be shared with the parent it
belongs to.

Nothing removes a link: it is a rebuildable projection, and a session that ends
leaves it pointing at a valid directory that the next launch of the chat
repoints. Only removing the chat or the runtime root removes it.
"""

from __future__ import annotations

import os
import time
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

import structlog

from meridian.lib.launch.cwd import resolve_effective_task_dir
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import (
    RuntimePaths,
    resolve_ambient_work_dir,
    resolve_project_paths,
    resolve_work_scratch_dir_for_project,
)

logger = structlog.get_logger(__name__)

_WORK_LINK = "work"
_TASK_LINK = "task"
_STAGED_SUFFIX = ".tmp"
# A publisher that crashed left its staged link behind; younger ones may belong
# to a live publisher, so leave those alone.
_STAGED_MAX_AGE_SECONDS = 3600.0


class LinkOwner(NamedTuple):
    """One live primary execution: the entry chat that owns its links, the chat it
    runs in now, and its run ID."""

    entry_chat: str
    current_chat: str
    spawn_id: str


def session_work_link_path(runtime_root: Path, chat_id: str) -> Path:
    """Return the stable work-directory link for one chat (pure path math)."""

    return _link_path(runtime_root, chat_id, _WORK_LINK)


def session_task_link_path(runtime_root: Path, chat_id: str) -> Path:
    """Return the stable source-checkout link for one chat (pure path math)."""

    return _link_path(runtime_root, chat_id, _TASK_LINK)


def _link_path(runtime_root: Path, chat_id: str, name: str) -> Path:
    return RuntimePaths.from_root_dir(runtime_root).sessions_dir / chat_id.strip() / name


def _attached_work_id(runtime_root: Path, chat_id: str) -> str:
    record = session_store.chat_record(runtime_root, chat_id)
    return ((record.active_work_id if record is not None else None) or "").strip()


def _session_work_target(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    *,
    ambient_spawn_id: str,
) -> Path:
    """Return the directory a chat's work link points at.

    One rule for launch and work commands: the chat's attached work item's
    directory, otherwise the ambient work directory of the primary spawn running
    the chat. The ambient directory is created so the link never dangles.
    """

    work_id = _attached_work_id(runtime_root, chat_id)
    if work_id:
        return resolve_work_scratch_dir_for_project(project_root, work_id)
    target = resolve_ambient_work_dir(project_root, ambient_spawn_id, runtime_root=runtime_root)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _session_task_target(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    *,
    ambient_spawn_id: str,
) -> Path:
    """Return the checkout a chat's task link points at.

    The same resolution a launch uses, minus the inherited directory: the
    primary's own spawn-scope task dir, then its work item's task dir, then the
    project root. The inherited ``MERIDIAN_TASK_DIR`` is the link itself, so
    following it would close the loop.
    """

    effective = resolve_effective_task_dir(
        project_root=project_root,
        project_state_dir=resolve_project_paths(project_root).root_dir,
        spawn_id=ambient_spawn_id,
        inherited_task_dir=None,
        work_id=_attached_work_id(runtime_root, chat_id) or None,
    )
    return effective.task_dir


def refresh_session_links(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    *,
    ambient_spawn_id: str,
    task_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Point one chat's links at its work attachment and its checkout.

    Both targets are read and swapped under the sessions lock, so a concurrent
    work command cannot leave a link on the state it just replaced. ``task_dir`` is
    a launch's resolved checkout: a primary launched with ``--task-dir`` names a
    directory no command would otherwise resolve.
    """

    with lock_file(RuntimePaths.from_root_dir(runtime_root).sessions_flock):
        work_link = _publish_session_link(
            runtime_root,
            chat_id,
            _WORK_LINK,
            _session_work_target(
                project_root,
                runtime_root,
                chat_id,
                ambient_spawn_id=ambient_spawn_id,
            ),
        )
        task_link = _publish_session_link(
            runtime_root,
            chat_id,
            _TASK_LINK,
            task_dir
            or _session_task_target(
                project_root,
                runtime_root,
                chat_id,
                ambient_spawn_id=ambient_spawn_id,
            ),
        )
    return work_link, task_link


def link_owners(runtime_root: Path, chat_id: str) -> tuple[LinkOwner, ...]:
    """Return the live primaries *chat_id* belongs to.

    A live primary can move off its entry chat, so any chat of the execution finds
    the entry chat that owns the links and whose path the launch environment and
    system prompt name.
    """

    normalized = chat_id.strip()
    if not normalized:
        return ()
    owners: list[LinkOwner] = []
    for owner in spawn_store.list_live_primaries(runtime_root):
        entry_chat = (owner.chat_id or "").strip()
        current_chat = (owner.continue_chat_id or "").strip()
        if entry_chat and normalized in (entry_chat, current_chat):
            owners.append(
                LinkOwner(
                    entry_chat=entry_chat,
                    current_chat=current_chat or entry_chat,
                    spawn_id=str(owner.id),
                )
            )
    return tuple(owners)


def refresh_primary_links(project_root: Path, runtime_root: Path, chat_id: str) -> None:
    """Repoint the links of every live primary execution *chat_id* belongs to.

    A spawn or a stopped session owns no link; both links are projections that the
    next launch of the chat rebuilds.
    """

    for owner in link_owners(runtime_root, chat_id):
        refresh_session_links(
            project_root,
            runtime_root,
            owner.entry_chat,
            ambient_spawn_id=owner.spawn_id,
        )


def _publish_session_link(runtime_root: Path, chat_id: str, name: str, target: Path) -> Path:
    """Point one chat link at *target*.

    Atomic swap: build the new link under a temporary name in the same directory,
    then rename it over the old one, so a crash leaves either the old link or the
    new one. A real file or directory in the slot — a harness that wrote the path
    instead of resolving it — is moved aside and logged, because the next swap can
    only replace a symlink.
    """

    link = _link_path(runtime_root, chat_id, name)
    link.parent.mkdir(parents=True, exist_ok=True)
    _sweep_staged_links(link, name)
    staged = link.parent / f"{name}.{os.getpid()}.{uuid4().hex}{_STAGED_SUFFIX}"
    os.symlink(target.as_posix(), staged)
    try:
        if os.path.lexists(link) and not link.is_symlink():
            displaced = link.with_name(f"{name}.displaced.{uuid4().hex}")
            os.rename(link, displaced)
            logger.warning(
                "Moved a real path out of a session link slot.",
                link=link.as_posix(),
                displaced=displaced.as_posix(),
            )
        os.replace(staged, link)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    return link


def _sweep_staged_links(link: Path, name: str) -> None:
    """Remove staged links a crashed publisher left behind."""

    cutoff = time.time() - _STAGED_MAX_AGE_SECONDS
    for staged in link.parent.glob(f"{name}.*{_STAGED_SUFFIX}"):
        with suppress(OSError):
            if staged.lstat().st_mtime < cutoff:
                staged.unlink()


__all__ = [
    "LinkOwner",
    "link_owners",
    "refresh_primary_links",
    "refresh_session_links",
    "session_task_link_path",
    "session_work_link_path",
]
