"""Shared CWD resolution for child spawn processes."""

from pathlib import Path


def resolve_child_execution_cwd(
    project_root: Path,
    spawn_id: str,
    harness_id: str,
    worktree_path: Path | None = None,
) -> Path:
    """Determine the actual CWD for a child spawn process.

    When worktree_path is provided and exists on disk, uses it as the CWD so
    the spawned agent operates inside the worktree rather than the main repo.
    Otherwise falls back to project_root (existing behavior).

    Note: MERIDIAN_PROJECT_DIR in the child env is NOT affected by this — it
    remains anchored to the original project root regardless of CWD routing.
    """
    _ = spawn_id, harness_id
    if worktree_path is not None and worktree_path.is_dir():
        return worktree_path
    return project_root
