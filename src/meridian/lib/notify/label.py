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


def build_session_label(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    resolve_tmux_session: TmuxSessionResolver = resolve_tmux_session,
) -> SessionLabel:
    """Resolve the optional actor, work, project, and tmux label parts."""
    env = os.environ if environ is None else environ
    working_directory = Path.cwd() if cwd is None else cwd
    pane = _clean(env.get("TMUX_PANE"))
    tmux_session = _clean(resolve_tmux_session(pane)) if pane else None
    project_path = _clean(env.get("MERIDIAN_PROJECT_DIR"))
    project = _clean(Path(project_path).name if project_path else working_directory.name)
    work_id = _clean(env.get("MERIDIAN_ACTIVE_WORK_ID"))
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
