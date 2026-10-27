"""Stable per-execution directory links under the runtime root.

Every live execution — a primary or a spawned agent — names two paths that
outlive any one launch, so they never go stale mid-run: ``sessions/<chat>/work``
is its work directory and ``sessions/<chat>/task`` is the checkout it edits. Both
are projections of durable state, never authority: the session row, the work item
and the spawn scope decide the targets, and every launch and every command that
moves those targets repoints them.

The links belong to an execution's *entry* chat, the one its session scope locks,
so they survive ``--continue`` of that chat, and a command on any chat of the
execution (Claude ``/clear`` or ``/resume`` rotates the conversation) finds that
one chat. A spawn has its own chat and so its own links; they are named in its
``# Meridian run`` user turn rather than the system prompt, so sibling spawns keep
sharing the cached prompt prefix.

Nothing removes a link: it is a rebuildable projection. A live execution's links
follow every move of its work directory and checkout; an execution that has ended
keeps the targets its row named at the end, so they can point at a directory a later
`work rename`, `work done` or `work reopen` moved away. The next launch of the chat
repoints them. Only removing the chat or the runtime root removes the link.
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
from meridian.lib.state import session_store, spawn_store, work_store
from meridian.lib.state.paths import (
    RuntimePaths,
    resolve_ambient_work_dir,
    resolve_project_paths,
)

logger = structlog.get_logger(__name__)

_WORK_LINK = "work"
_TASK_LINK = "task"
_STAGED_SUFFIX = ".tmp"
# A publisher that crashed left its staged link behind; younger ones may belong
# to a live publisher, so leave those alone.
_STAGED_MAX_AGE_SECONDS = 3600.0


class LinkOwner(NamedTuple):
    """One live execution: the chat its session row lives on, the chat whose links
    it owns, its run ID, and the work item it is attached to."""

    chat: str
    link_chat: str
    spawn_id: str
    kind: str
    work_id: str | None


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
    return ((row.chat_id or "") if row is not None else "").strip()


def _owner_of(runtime_root: Path, record: session_store.SessionRecord) -> LinkOwner:
    """One session record as a link owner.

    A primary's run row, not the chat a command happens to run on, says where the
    execution is now and which chat owns its links: Claude ``/clear`` moves it to a new
    chat while the process keeps running on the old one, whose lease it still holds. The
    new chat inherits the attachment and every later write touches both rows, so the
    record's own attachment is the execution's.
    """

    spawn_id = (record.spawn_id or "").strip()
    chat = record.chat_id
    link_chat = record.chat_id
    if record.kind == "primary" and spawn_id:
        row = spawn_store.get_spawn(runtime_root, spawn_id)
        if row is not None:
            link_chat = (row.chat_id or "").strip() or link_chat
            chat = (row.continue_chat_id or "").strip() or chat
    return LinkOwner(
        chat=chat,
        link_chat=link_chat,
        spawn_id=spawn_id,
        kind=record.kind,
        work_id=record.active_work_id,
    )


def execution_owners(runtime_root: Path) -> tuple[LinkOwner, ...]:
    """Return one owner per live execution, from the live session records.

    One index read and one spawn read per live primary is the whole cost. Reading each
    chat's record separately would re-scan the session journal, and scanning every
    spawn file would grow with the project's history.
    """

    owners: dict[str, LinkOwner] = {}
    for record in session_store.list_active_session_records(runtime_root):
        if not (record.spawn_id or "").strip():
            continue
        owner = _owner_of(runtime_root, record)
        owners.setdefault(owner.link_chat, owner)
    return tuple(owners.values())


def link_owners(runtime_root: Path, chat_id: str) -> tuple[LinkOwner, ...]:
    """Return the live executions *chat_id* is a conversation of."""

    normalized = chat_id.strip()
    if not normalized:
        return ()
    return tuple(
        owner
        for owner in execution_owners(runtime_root)
        if normalized in (owner.chat, owner.link_chat)
    )


def work_owners(runtime_root: Path, work_id: str) -> tuple[LinkOwner, ...]:
    """Return the live executions attached to one work item."""

    normalized = work_id.strip()
    if not normalized:
        return ()
    return tuple(
        owner for owner in execution_owners(runtime_root) if owner.work_id == normalized
    )


def refresh_links_for_chat(project_root: Path, runtime_root: Path, chat_id: str) -> None:
    """Repoint the links of every live execution *chat_id* is a conversation of."""

    refresh_owner_links(project_root, runtime_root, link_owners(runtime_root, chat_id))


def refresh_owner_links(
    project_root: Path,
    runtime_root: Path,
    owners: tuple[LinkOwner, ...],
) -> None:
    """Repoint the links of the live executions in *owners*."""

    for owner in owners:
        refresh_session_links(
            project_root,
            runtime_root,
            owner.link_chat,
            ambient_spawn_id=owner.spawn_id,
        )


def refresh_session_links(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    *,
    ambient_spawn_id: str,
) -> tuple[Path, Path]:
    """Point one chat's links at its work attachment and its checkout.

    Both targets are read and swapped under the sessions lock, so a concurrent
    work command cannot leave a link on the state it just replaced.
    """

    # The one lock every row write and link swap takes: a concurrent `work switch`
    # cannot leave a link on the attachment it just replaced.
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
            _session_task_target(
                project_root,
                runtime_root,
                chat_id,
                ambient_spawn_id=ambient_spawn_id,
            ),
        )
    return work_link, task_link


def _attached_work_id(runtime_root: Path, chat_id: str) -> str:
    return (session_store.get_session_active_work_id(runtime_root, chat_id) or "").strip()


def _session_work_target(
    project_root: Path,
    runtime_root: Path,
    chat_id: str,
    *,
    ambient_spawn_id: str,
) -> Path:
    """Return the directory a chat's work link points at.

    One rule for launch and work commands: the chat's attached work item's
    directory — whichever of ``work/`` and the archive currently holds it — or,
    with no item, the ambient work directory of the execution running the chat. The
    ambient directory is created so the link never dangles.
    """

    work_id = _attached_work_id(runtime_root, chat_id)
    if work_id:
        return work_store.work_scratch_dir(resolve_project_paths(project_root).root_dir, work_id)
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

    The same resolution a launch uses, from durable state only: the execution's own
    spawn-scope task dir — written at bind when the launch resolved a ``--task-dir``,
    inherited one, or the caller's directory — then its work item's task dir, then
    the project root. The inherited ``MERIDIAN_TASK_DIR`` is the link itself, so
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
    "execution_link_chat",
    "execution_owners",
    "link_owners",
    "refresh_links_for_chat",
    "refresh_owner_links",
    "refresh_session_links",
    "session_task_link_path",
    "session_work_link_path",
    "work_owners",
]
