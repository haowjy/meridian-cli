"""Stable per-session work-directory link under the runtime root.

The link is a projection of one chat's active work attachment, never authority:
``sessions.jsonl`` and the work-item directory decide the target, and every
primary launch rebuilds a missing link before naming it. Its path never changes,
so ``MERIDIAN_ACTIVE_WORK_DIR`` and the system prompt can name it and stay
correct across every work-attachment change.

``work done``/``delete`` detach the chat and point the link at its ambient spawn
directory, so the stable path stays writable with no active item. A *dangling*
link means the target disappeared outside that seam (an older build archived it,
or someone removed the directory); resolution then reports no active work item
instead of serving the dead path.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from meridian.lib.state.paths import RuntimePaths

_WORK_LINK_NAME = "work"
_STAGED_SUFFIX = ".tmp"


def session_work_link_path(runtime_root: Path, chat_id: str) -> Path:
    """Return the stable work-directory link for one chat (pure path math)."""

    return RuntimePaths.from_root_dir(runtime_root).sessions_dir / chat_id.strip() / _WORK_LINK_NAME


def publish_session_work_link(
    runtime_root: Path,
    chat_id: str,
    target: Path | None,
) -> Path | None:
    """Point a chat's work link at *target*, or remove the link when ``None``.

    Atomic swap: create the new link under a temporary name in the same
    directory, then rename it over the old one. A crash leaves the old link or
    the new link, never a partially built one. Removing is idempotent, so a
    missing link (a session created by an older build) is not an error.
    """

    normalized = chat_id.strip()
    if not normalized:
        return None
    link = session_work_link_path(runtime_root, normalized)
    if target is None:
        for stale in (link, *link.parent.glob(f"{_WORK_LINK_NAME}.*{_STAGED_SUFFIX}")):
            stale.unlink(missing_ok=True)
        # Another session file still living here, or a concurrent writer that just
        # recreated the link, means the directory is not ours to remove.
        with suppress(OSError):
            link.parent.rmdir()
        return None

    link.parent.mkdir(parents=True, exist_ok=True)
    staged = link.parent / f"{_WORK_LINK_NAME}.{os.getpid()}.{uuid4().hex}{_STAGED_SUFFIX}"
    os.symlink(target.as_posix(), staged)
    try:
        os.replace(staged, link)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    return link


__all__ = ["publish_session_work_link", "session_work_link_path"]
