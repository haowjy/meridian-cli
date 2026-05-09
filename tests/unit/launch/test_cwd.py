from pathlib import Path

from meridian.lib.launch.cwd import resolve_child_execution_cwd
from meridian.lib.state.work_store import create_work_item, update_work_item_worktree


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def test_resolve_child_execution_cwd_prefers_worktree_from_work_item(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    item = create_work_item(runtime_root, "feature-a")
    worktree_path = tmp_path / "repo.worktrees" / item.name
    worktree_path.mkdir(parents=True)
    update_work_item_worktree(runtime_root, item.name, path=str(worktree_path))

    resolved = resolve_child_execution_cwd(
        project_root,
        project_state_dir=runtime_root,
        work_id=item.name,
    )

    assert resolved == worktree_path


def test_resolve_child_execution_cwd_falls_back_when_worktree_missing(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    item = create_work_item(runtime_root, "feature-a")
    missing_worktree = tmp_path / "repo.worktrees" / item.name
    update_work_item_worktree(runtime_root, item.name, path=str(missing_worktree))

    resolved = resolve_child_execution_cwd(
        project_root,
        project_state_dir=runtime_root,
        work_id=item.name,
    )

    assert resolved == project_root
