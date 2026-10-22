"""Build notification labels from the current terminal and work context."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from meridian.lib.notify.notice import SessionLabel
from meridian.lib.platform.tmux import resolve_tmux_session

TmuxSessionResolver = Callable[[str], str | None]
RuntimeRootResolver = Callable[[Path], Path | None]
SessionWorkResolver = Callable[[Path, str], tuple[bool, str | None]]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _resolve_runtime_root(project_root: Path) -> Path | None:
    from meridian.lib.state.paths import resolve_project_runtime_root_or_none

    return resolve_project_runtime_root_or_none(project_root)


def _resolve_session_work(runtime_root: Path, chat_id: str) -> tuple[bool, str | None]:
    from meridian.lib.state.session_store import chat_record

    record = chat_record(runtime_root, chat_id)
    return (record is not None, record.active_work_id if record is not None else None)


def build_session_label(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    resolve_tmux_session: TmuxSessionResolver = resolve_tmux_session,
    resolve_runtime_root: RuntimeRootResolver = _resolve_runtime_root,
    resolve_session_work: SessionWorkResolver = _resolve_session_work,
) -> SessionLabel:
    """Resolve the optional actor, current work, project, and tmux label parts."""
    env = os.environ if environ is None else environ
    working_directory = Path.cwd() if cwd is None else cwd
    pane = _clean(env.get("TMUX_PANE"))
    tmux_session = _clean(resolve_tmux_session(pane)) if pane else None
    project_path = _clean(env.get("MERIDIAN_PROJECT_DIR"))
    project_root = Path(project_path) if project_path else working_directory
    project = _clean(project_root.name)
    fallback_work_id = _clean(env.get("MERIDIAN_ACTIVE_WORK_ID"))
    work_id = fallback_work_id
    chat_id = _clean(env.get("MERIDIAN_CHAT_ID"))
    if chat_id:
        try:
            runtime_root = resolve_runtime_root(project_root)
            resolved, current_work_id = (
                resolve_session_work(runtime_root, chat_id)
                if runtime_root is not None
                else (False, None)
            )
        except (OSError, ValueError):
            resolved = False
            current_work_id = None
        if resolved:
            work_id = _clean(current_work_id)
    agent = _clean(env.get("MERIDIAN_SESSION_AGENT"))
    harness = _clean(env.get("_MERIDIAN_HARNESS"))
    return SessionLabel(
        tmux_session=tmux_session,
        project=project,
        work_id=work_id,
        agent=agent,
        harness=harness,
    )


__all__ = ["build_session_label"]
