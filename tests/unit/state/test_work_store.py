from __future__ import annotations

from meridian.lib.state.work_state import WorktreeMetadata


def test_legacy_worktree_metadata_normalizes_windows_path_separators() -> None:
    metadata = WorktreeMetadata(
        path=r"C:\Users\dev\repo\.worktrees\feature-x",
        repo_path=r"C:\Users\dev\repo",
    )

    assert metadata.path == "C:/Users/dev/repo/.worktrees/feature-x"
    assert metadata.repo_path == "C:/Users/dev/repo"
