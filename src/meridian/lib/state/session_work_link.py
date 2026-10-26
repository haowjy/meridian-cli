"""Stable per-session work-directory link under the runtime root.

The link is a projection of a primary chat's work attachment, never authority:
the session row and the work item decide the target, and every primary launch
rebuilds the link before naming it. Its path never changes, so
``MERIDIAN_ACTIVE_WORK_DIR`` and the system prompt can name it and stay correct
across ``work start``/``switch``/``rename``, including a Claude ``/clear`` that
moves the conversation to a new chat.

The link belongs to a primary execution's *entry* chat, the one its session scope
locks, so it survives ``--continue`` of that chat. ``work done``/``delete`` detach
the chat and point the link at the primary spawn's ambient directory, so the
stable path stays writable with no active item.

Nothing removes the link: it is a rebuildable projection, and a session that ends
leaves it pointing at a valid directory that the next launch of the chat
repoints.
"""

from __future__ import annotations

import os
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import structlog

from meridian.lib.state.paths import (
    RuntimePaths,
    resolve_ambient_work_dir,
    resolve_work_scratch_dir_for_project,
)

logger = structlog.get_logger(__name__)

_WORK_LINK_NAME = "work"
_STAGED_SUFFIX = ".tmp"
# A publisher that crashed left its staged link behind; younger ones may belong
# to a live publisher, so leave those alone.
_STAGED_MAX_AGE_SECONDS = 3600.0


def session_work_link_path(runtime_root: Path, chat_id: str) -> Path:
    """Return the stable work-directory link for one chat (pure path math)."""

    return RuntimePaths.from_root_dir(runtime_root).sessions_dir / chat_id.strip() / _WORK_LINK_NAME


def session_work_target(
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

    from meridian.lib.state import session_store

    record = session_store.chat_record(runtime_root, chat_id)
    work_id = (record.active_work_id if record is not None else None) or ""
    if work_id.strip():
        return resolve_work_scratch_dir_for_project(project_root, work_id)
    target = resolve_ambient_work_dir(project_root, ambient_spawn_id, runtime_root=runtime_root)
    target.mkdir(parents=True, exist_ok=True)
    return target


def publish_session_work_link(runtime_root: Path, chat_id: str, target: Path) -> Path:
    """Point a chat's work link at *target*.

    Atomic swap: build the new link under a temporary name in the same directory,
    then rename it over the old one, so a crash leaves either the old link or the
    new one. A real file or directory in the slot — a harness that wrote the path
    instead of resolving it — is moved aside and logged, because the next swap
    can only replace a symlink.
    """

    link = session_work_link_path(runtime_root, chat_id)
    link.parent.mkdir(parents=True, exist_ok=True)
    _sweep_staged_links(link)
    staged = link.parent / f"{_WORK_LINK_NAME}.{os.getpid()}.{uuid4().hex}{_STAGED_SUFFIX}"
    os.symlink(target.as_posix(), staged)
    try:
        if os.path.lexists(link) and not link.is_symlink():
            displaced = link.with_name(f"{_WORK_LINK_NAME}.displaced.{uuid4().hex}")
            os.rename(link, displaced)
            logger.warning(
                "Moved a real path out of the session work link slot.",
                link=link.as_posix(),
                displaced=displaced.as_posix(),
            )
        os.replace(staged, link)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    return link


def _sweep_staged_links(link: Path) -> None:
    """Remove staged links a crashed publisher left behind."""

    cutoff = time.time() - _STAGED_MAX_AGE_SECONDS
    for staged in link.parent.glob(f"{_WORK_LINK_NAME}.*{_STAGED_SUFFIX}"):
        with suppress(OSError):
            if staged.lstat().st_mtime < cutoff:
                staged.unlink()


__all__ = [
    "publish_session_work_link",
    "session_work_link_path",
    "session_work_target",
]
