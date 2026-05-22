from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.launch.cwd import resolve_task_cwd
from meridian.lib.ops import work_lifecycle
from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.work_lifecycle import (
    WorkClearWorktreeInput,
    WorkDeleteInput,
    WorkDoneInput,
    WorkRenameInput,
    WorkReopenInput,
    WorkSetWorktreeInput,
    work_clear_worktree_sync,
    work_delete_sync,
    work_done_sync,
    work_rename_sync,
    work_reopen_sync,
    work_set_worktree_sync,
)
from meridian.lib.ops.worktree_lifecycle import WorktreeRestoreResult
from meridian.lib.state import work_store
from meridian.lib.state.work_store import WorkItem, WorktreeMetadata

if TYPE_CHECKING:
    import pytest


def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    return project_root, roots.project_state_dir


def test_work_set_and_clear_worktree(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    work = work_store.create_work_item(project_state_dir, "feature-x", "", None)
    worktree_path = tmp_path / "feature-x-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)

    set_output = work_set_worktree_sync(
        WorkSetWorktreeInput(
            work_id=work.name,
            path=worktree_path.as_posix(),
            project_root=project_root.as_posix(),
        )
    )
    assert set_output.worktree_path == worktree_path.resolve().as_posix()
    item = work_store.get_work_item(project_state_dir, work.name)
    assert item is not None
    assert item.worktree_path == worktree_path.resolve().as_posix()
    assert item.worktree_managed is False

    work_store.update_work_item_worktree(
        project_state_dir,
        work.name,
        branch="feature/stale-branch",
    )

    clear_output = work_clear_worktree_sync(
        WorkClearWorktreeInput(work_id=work.name, project_root=project_root.as_posix())
    )
    assert clear_output.work_id == work.name
    item = work_store.get_work_item(project_state_dir, work.name)
    assert item is not None
    assert item.worktree_path is None
    assert item.worktree_branch is None
    assert item.worktree_managed is False


def test_work_clear_worktree_makes_spawn_resolution_fall_back_to_authority_root(
    tmp_path: Path,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    work = work_store.create_work_item(project_state_dir, "feature-work", "", None)
    worktree_path = tmp_path / "feature-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(
        project_state_dir,
        work.name,
        path=worktree_path.as_posix(),
    )

    work_clear_worktree_sync(
        WorkClearWorktreeInput(work_id=work.name, project_root=project_root.as_posix())
    )
    resolved = resolve_task_cwd(
        project_root,
        project_state_dir=project_state_dir,
        explicit_work_id=work.name,
    )

    assert resolved.task_cwd == project_root.resolve()
    assert resolved.source == "explicit-work-authority-root"


def test_manual_worktree_assignment_done_and_delete_do_not_remove_path(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    done_item = work_store.create_work_item(project_state_dir, "manual-done", "", None)
    delete_item = work_store.create_work_item(project_state_dir, "manual-delete", "", None)
    shared_manual_path = tmp_path / "manual-assignment"
    shared_manual_path.mkdir(parents=True, exist_ok=True)

    work_set_worktree_sync(
        WorkSetWorktreeInput(
            work_id=done_item.name,
            path=shared_manual_path.as_posix(),
            project_root=project_root.as_posix(),
        )
    )
    work_set_worktree_sync(
        WorkSetWorktreeInput(
            work_id=delete_item.name,
            path=shared_manual_path.as_posix(),
            project_root=project_root.as_posix(),
        )
    )

    done_output = work_done_sync(
        WorkDoneInput(work_id=done_item.name, project_root=project_root.as_posix())
    )
    assert shared_manual_path.is_dir()
    assert "manually assigned" in (done_output.warning or "")

    delete_output = work_delete_sync(
        WorkDeleteInput(work_id=delete_item.name, force=False, project_root=project_root.as_posix())
    )
    assert delete_output.deleted is True
    assert shared_manual_path.is_dir()
    assert "manually assigned" in delete_output.warning


def test_manual_worktree_assignment_rename_does_not_move_path(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "manual-rename", "", None)
    manual_path = tmp_path / "manual-rename-path"
    manual_path.mkdir(parents=True, exist_ok=True)
    work_set_worktree_sync(
        WorkSetWorktreeInput(
            work_id=item.name,
            path=manual_path.as_posix(),
            project_root=project_root.as_posix(),
        )
    )

    result = work_rename_sync(
        WorkRenameInput(
            work_id=item.name,
            new_name="manual-renamed",
            rename_worktree=True,
            project_root=project_root.as_posix(),
        )
    )

    assert result.changed is True
    assert result.worktree_moved is False
    assert "manually assigned" in (result.warning or "")
    assert manual_path.is_dir()
    renamed = work_store.get_work_item(project_state_dir, "manual-renamed")
    assert renamed is not None
    assert renamed.worktree_path == manual_path.resolve().as_posix()
    assert renamed.worktree_managed is False


def test_shared_managed_worktree_done_skips_removal(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item_a = work_store.create_work_item(project_state_dir, "managed-a", "", None)
    item_b = work_store.create_work_item(project_state_dir, "managed-b", "", None)
    shared_path = tmp_path / "shared-managed"
    shared_path.mkdir(parents=True, exist_ok=True)

    work_store.update_work_item_worktree(
        project_state_dir,
        item_a.name,
        path=shared_path.as_posix(),
        branch="feature/managed-a",
        managed=True,
    )
    work_store.update_work_item_worktree(
        project_state_dir,
        item_b.name,
        path=shared_path.as_posix(),
        branch="feature/managed-b",
        managed=True,
    )

    done_output = work_done_sync(
        WorkDoneInput(work_id=item_a.name, project_root=project_root.as_posix())
    )
    assert shared_path.is_dir()
    assert "still referenced by work item(s): managed-b" in (done_output.warning or "")


def test_shared_managed_worktree_rename_skips_move(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item_a = work_store.create_work_item(project_state_dir, "rename-a", "", None)
    item_b = work_store.create_work_item(project_state_dir, "rename-b", "", None)
    shared_path = tmp_path / "shared-rename"
    shared_path.mkdir(parents=True, exist_ok=True)

    work_store.update_work_item_worktree(
        project_state_dir,
        item_a.name,
        path=shared_path.as_posix(),
        branch="feature/rename-a",
        managed=True,
    )
    work_store.update_work_item_worktree(
        project_state_dir,
        item_b.name,
        path=shared_path.as_posix(),
        branch="feature/rename-b",
        managed=True,
    )

    result = work_rename_sync(
        WorkRenameInput(
            work_id=item_a.name,
            new_name="rename-a-new",
            rename_worktree=True,
            project_root=project_root.as_posix(),
        )
    )

    assert result.changed is True
    assert result.worktree_moved is False
    assert "still referenced by work item(s): rename-b" in (result.warning or "")
    assert shared_path.is_dir()


def test_managed_unshared_done_keeps_cleanup_lifecycle_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "managed-cleanup", "", None)
    managed_path = tmp_path / "managed-cleanup-path"
    managed_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=managed_path.as_posix(),
        branch="feature/managed-cleanup",
        managed=True,
    )

    from meridian.lib.ops import worktree_lifecycle

    remove_calls: list[tuple[Path, Path, bool]] = []

    def _no_unpushed(_path: Path) -> None:
        return None

    def _repo_root(_path: Path) -> Path:
        return project_root

    monkeypatch.setattr(
        worktree_lifecycle,
        "ensure_no_unpushed_commits",
        _no_unpushed,
    )
    monkeypatch.setattr(
        worktree_lifecycle,
        "resolve_main_repo_root",
        _repo_root,
    )

    def _fake_remove(repo_root: Path, worktree_path: Path, *, force: bool = False) -> None:
        remove_calls.append((repo_root, worktree_path, force))

    monkeypatch.setattr(worktree_lifecycle, "remove_worktree", _fake_remove)

    done_output = work_done_sync(
        WorkDoneInput(work_id=item.name, project_root=project_root.as_posix())
    )
    assert "Removed worktree at" in (done_output.warning or "")
    assert len(remove_calls) == 1
    assert remove_calls[0][1] == managed_path.resolve()


def test_work_reopen_missing_worktree_notice_requires_fix_not_silent_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "reopen-work", "", None)
    work_store.update_work_item_worktree(
        project_state_dir,
        item.name,
        path=(tmp_path / "missing-reopen-worktree").as_posix(),
        managed=True,
    )
    work_store.archive_work_item(project_state_dir, item.name)

    def _fallback_result(
        _project_root: Path, archived_item: WorkItem
    ) -> WorktreeRestoreResult:
        return WorktreeRestoreResult(
            status="fallback_project_root",
            metadata=WorktreeMetadata(
                path=archived_item.worktree_path,
                branch=archived_item.worktree_branch,
                pending=False,
                managed=True,
            ),
        )

    monkeypatch.setattr(work_lifecycle, "restore_for_reopen", _fallback_result)

    reopened = work_reopen_sync(
        WorkReopenInput(work_id=item.name, project_root=project_root.as_posix())
    )

    assert reopened.status == "open"
    assert reopened.warning is not None
    assert "--no-worktree" in reopened.warning
    assert "use the project root for CWD" not in reopened.warning
