"""Build notification labels from the current terminal and work context."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from meridian.lib.notify.notice import SessionLabel
from meridian.lib.platform.tmux import resolve_tmux_session

TmuxSessionResolver = Callable[[str], str | None]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _current_work_id(env: Mapping[str, str], project_root: Path) -> str | None:
    """The work the sender is on now; the launch value when state can't be read."""

    import sqlite3

    from meridian.lib.core.resolved_context import LocalFilesystemBackend, follow_current_chat
    from meridian.lib.state.history_index import HistoryIndexIncomplete
    from meridian.lib.state.paths import resolve_project_runtime_root_or_none

    launch_work_id = _clean(env.get("MERIDIAN_ACTIVE_WORK_ID"))
    try:
        runtime_root = resolve_project_runtime_root_or_none(project_root)
        if runtime_root is None:
            return launch_work_id
        current_chat_id = follow_current_chat(env, runtime_root)
        chat_id = current_chat_id or _clean(env.get("MERIDIAN_CHAT_ID"))
        if launch_work_id and not current_chat_id:
            return launch_work_id
        if not chat_id:
            return None
        return _clean(LocalFilesystemBackend().get_session_active_work_id(runtime_root, chat_id))
    except (OSError, ValueError, HistoryIndexIncomplete, sqlite3.Error):
        return launch_work_id


def build_session_label(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    resolve_tmux_session: TmuxSessionResolver = resolve_tmux_session,
) -> SessionLabel:
    """Resolve the optional actor, current work, project, and tmux label parts."""
    env = os.environ if environ is None else environ
    working_directory = Path.cwd() if cwd is None else cwd
    pane = _clean(env.get("TMUX_PANE"))
    tmux_session = _clean(resolve_tmux_session(pane)) if pane else None
    project_path = _clean(env.get("MERIDIAN_PROJECT_DIR"))
    project_root = Path(project_path) if project_path else working_directory
    return SessionLabel(
        tmux_session=tmux_session,
        project=_clean(project_root.name),
        work_id=_current_work_id(env, project_root),
        agent=_clean(env.get("MERIDIAN_SESSION_AGENT")),
        harness=_clean(env.get("_MERIDIAN_HARNESS")),
    )


__all__ = ["build_session_label"]
