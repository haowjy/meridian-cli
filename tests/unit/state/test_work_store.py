from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.state import work_store
from meridian.lib.state.work_store import WorktreeMetadata


def test_worktree_metadata_normalizes_windows_path_separators() -> None:
    metadata = WorktreeMetadata(
        path=r"C:\Users\dev\repo\.worktrees\feature-x",
        repo_path=r"C:\Users\dev\repo",
    )

    assert metadata.path == "C:/Users/dev/repo/.worktrees/feature-x"
    assert metadata.repo_path == "C:/Users/dev/repo"


def test_update_work_item_worktree_persists_normalized_worktree_paths(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    item = work_store.create_work_item(state_root, "feature-x", "", None)

    updated = work_store.update_work_item_worktree(
        state_root,
        item.name,
        path=r"C:\Users\dev\repo\.worktrees\feature-x",
        repo_path=r"C:\Users\dev\repo",
        managed=True,
    )

    assert updated.worktree_path == "C:/Users/dev/repo/.worktrees/feature-x"
    assert updated.worktree_repo_path == "C:/Users/dev/repo"

    status_path = work_store.work_scratch_dir(state_root, item.name) / "__status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["worktree"]["path"] == "C:/Users/dev/repo/.worktrees/feature-x"
    assert payload["worktree"]["repo_path"] == "C:/Users/dev/repo"
