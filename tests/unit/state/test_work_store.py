from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.state import work_store
from meridian.lib.state.work_store import WorktreeMetadata


def test_legacy_worktree_metadata_normalizes_windows_path_separators() -> None:
    metadata = WorktreeMetadata(
        path=r"C:\Users\dev\repo\.worktrees\feature-x",
        repo_path=r"C:\Users\dev\repo",
    )

    assert metadata.path == "C:/Users/dev/repo/.worktrees/feature-x"
    assert metadata.repo_path == "C:/Users/dev/repo"


def test_get_work_item_lazily_migrates_task_dir_from_legacy_worktree_path(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    item = work_store.create_work_item(state_root, "legacy-task-dir", "", None)
    legacy_dir = tmp_path / "legacy-worktree"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    status_path = work_store.work_scratch_dir(state_root, item.name) / "__status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["task_dir"] = None
    payload["worktree"]["path"] = legacy_dir.as_posix()
    status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    loaded = work_store.get_work_item(state_root, item.name)
    assert loaded is not None
    assert loaded.task_dir == legacy_dir.resolve().as_posix()

    payload_after = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload_after["task_dir"] == legacy_dir.resolve().as_posix()
    assert payload_after["worktree"]["path"] == legacy_dir.resolve().as_posix()


def test_update_work_item_task_dir_persists_normalized_path(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    item = work_store.create_work_item(state_root, "feature-task-dir", "", None)
    task_dir = tmp_path / "target" / "nested"
    task_dir.mkdir(parents=True, exist_ok=True)

    updated = work_store.update_work_item_task_dir(
        state_root,
        item.name,
        task_dir=task_dir.as_posix(),
    )
    assert updated.task_dir == task_dir.resolve().as_posix()

    status_path = work_store.work_scratch_dir(state_root, item.name) / "__status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["task_dir"] == task_dir.resolve().as_posix()
