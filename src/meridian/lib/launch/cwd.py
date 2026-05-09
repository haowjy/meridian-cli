"""Shared child-process CWD policy for spawn launches."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state import work_store


def resolve_child_execution_cwd(
    project_root: Path,
    *,
    project_state_dir: Path | None = None,
    work_id: str | None = None,
    worktree_path: Path | None = None,
) -> Path:
    """Determine the child spawn CWD, preferring an active worktree when present."""
    candidate = worktree_path
    if candidate is None and project_state_dir is not None and work_id:
        item = work_store.get_work_item(project_state_dir, work_id)
        if item is not None and item.worktree_path is not None:
            candidate = Path(item.worktree_path)
    if candidate is not None and candidate.is_dir():
        return candidate
    return project_root
