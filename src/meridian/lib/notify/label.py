"""Build notification labels from the current terminal and work context."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from meridian.lib.notify.notice import SessionLabel

TmuxSessionResolver = Callable[[str], str | None]


def _tmux_session(pane: str) -> str | None:
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#S"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def build_session_label(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    resolve_tmux_session: TmuxSessionResolver = _tmux_session,
) -> SessionLabel:
    """Resolve the optional tmux, project, and work-id label parts."""
    env = os.environ if environ is None else environ
    working_directory = Path.cwd() if cwd is None else cwd
    pane = _clean(env.get("TMUX_PANE"))
    tmux_session = _clean(resolve_tmux_session(pane)) if pane else None
    project_path = _clean(env.get("MERIDIAN_PROJECT_DIR"))
    project = _clean(Path(project_path).name if project_path else working_directory.name)
    work_id = _clean(env.get("MERIDIAN_ACTIVE_WORK_ID"))
    return SessionLabel(tmux_session=tmux_session, project=project, work_id=work_id)


__all__ = ["build_session_label"]
