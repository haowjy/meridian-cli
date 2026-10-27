"""Build notification labels from the current terminal and work context."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.notify.notice import SessionLabel
from meridian.lib.platform.tmux import resolve_tmux_session

if TYPE_CHECKING:
    from meridian.lib.state.spawn.model import SpawnRecord

TmuxSessionResolver = Callable[[str], str | None]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


_MAX_ANCESTRY = 32


def _primary_of(runtime_root: Path, spawn_id: str) -> SpawnRecord | None:
    """The primary at the root of a spawn's parent chain, or None."""

    from meridian.lib.state import spawn_store

    seen: set[str] = set()
    current: str | None = spawn_id
    while current and current not in seen and len(seen) < _MAX_ANCESTRY:
        seen.add(current)
        row = spawn_store.get_spawn(runtime_root, current)
        if row is None:
            return None
        if row.kind == "primary":
            return row
        current = row.parent_id
    return None


def build_session_label(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    resolve_tmux_session: TmuxSessionResolver = resolve_tmux_session,
) -> SessionLabel:
    """Name the primary session a notification belongs to.

    A spawn inherits its primary's terminal (``TMUX_PANE``); its agent, harness
    and work come from the primary's spawn record and current session, and the
    spawn itself is named as the child. Unreadable state falls back to the
    launch environment, so a notification never fails on it.
    """
    env = os.environ if environ is None else environ
    working_directory = Path.cwd() if cwd is None else cwd
    pane = _clean(env.get("TMUX_PANE"))
    tmux_session = _clean(resolve_tmux_session(pane)) if pane else None
    project_path = _clean(env.get("MERIDIAN_PROJECT_DIR"))
    project_root = Path(project_path) if project_path else working_directory
    agent = _clean(env.get("MERIDIAN_SESSION_AGENT"))
    harness = _clean(env.get("_MERIDIAN_HARNESS"))
    work_id = _clean(env.get("MERIDIAN_ACTIVE_WORK_ID"))
    child_spawn_id: str | None = None
    child_agent: str | None = None
    child_harness: str | None = None
    # State modules load lazily: the label is built on `meridian notify` startup.
    from meridian.lib.ops.work_attachment import resolve_active_work_id
    from meridian.lib.state.history_index import HistoryIndexIncomplete
    from meridian.lib.state.paths import resolve_project_runtime_root_or_none

    try:
        runtime_root = resolve_project_runtime_root_or_none(project_root)
        spawn_id = _clean(env.get("MERIDIAN_SPAWN_ID"))
        if runtime_root is not None:
            primary = _primary_of(runtime_root, spawn_id) if spawn_id else None
            if primary is not None and primary.id != spawn_id:
                child_spawn_id, child_agent, child_harness = spawn_id, agent, harness
                agent, harness = primary.agent, primary.harness
                work_id = (
                    resolve_active_work_id(
                        project_root,
                        runtime_root,
                        chat_id=primary.continue_chat_id,
                    )
                    or work_id
                )
            else:
                work_id = (
                    resolve_active_work_id(project_root, runtime_root, environ=env) or work_id
                )
    except (OSError, ValueError, HistoryIndexIncomplete, sqlite3.Error):
        pass
    return SessionLabel(
        tmux_session=tmux_session,
        project=_clean(project_root.name),
        work_id=work_id,
        agent=agent,
        harness=harness,
        child_spawn_id=child_spawn_id,
        child_agent=child_agent,
        child_harness=child_harness,
    )


__all__ = ["build_session_label"]
