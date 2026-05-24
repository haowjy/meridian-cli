from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.work_lifecycle import (
    WorkClearWorktreeInput,
    WorkDeleteInput,
    WorkDoneInput,
    WorkRenameInput,
    WorkSetWorktreeInput,
    WorkStartInput,
    WorkWorktreeInput,
    work_clear_worktree_sync,
    work_delete_sync,
    work_done_sync,
    work_rename_sync,
    work_set_worktree_sync,
    work_start_sync,
    work_worktree_sync,
)
from meridian.lib.state import work_store


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


def test_manual_worktree_assignment_done_and_delete_do_not_remove_path(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    done_item = work_store.create_work_item(project_state_dir, "manual-done", "", None)
    delete_item = work_store.create_work_item(project_state_dir, "manual-delete", "", None)
    shared_manual_path = tmp_path / "manual-assignment"
    shared_manual_path.mkdir(parents=True, exist_ok=True)

    for item in (done_item, delete_item):
        work_set_worktree_sync(
            WorkSetWorktreeInput(
                work_id=item.name,
                path=shared_manual_path.as_posix(),
                project_root=project_root.as_posix(),
            )
        )

    done_output = work_done_sync(
        WorkDoneInput(work_id=done_item.name, project_root=project_root.as_posix())
    )
    delete_output = work_delete_sync(
        WorkDeleteInput(work_id=delete_item.name, force=False, project_root=project_root.as_posix())
    )

    assert shared_manual_path.is_dir()
    assert "manually assigned" in (done_output.warning or "")
    assert delete_output.deleted is True
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


def test_shared_managed_worktree_archived_peer_does_not_block_last_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item_a = work_store.create_work_item(project_state_dir, "managed-peer-a", "", None)
    item_b = work_store.create_work_item(project_state_dir, "managed-peer-b", "", None)
    shared_path = tmp_path / "shared-managed-final-cleanup"
    shared_path.mkdir(parents=True, exist_ok=True)

    for item in (item_a, item_b):
        work_store.update_work_item_worktree(
            project_state_dir,
            item.name,
            path=shared_path.as_posix(),
            branch=f"feature/{item.name}",
            managed=True,
        )

    from meridian.lib.ops import worktree_lifecycle

    remove_calls: list[tuple[Path, Path, bool]] = []

    monkeypatch.setattr(worktree_lifecycle, "resolve_main_repo_root", lambda _path: project_root)
    monkeypatch.setattr(worktree_lifecycle, "ensure_no_unpushed_commits", lambda _path: None)

    def _fake_remove(repo_root: Path, worktree_path: Path, *, force: bool = False) -> None:
        remove_calls.append((repo_root, worktree_path, force))

    monkeypatch.setattr(worktree_lifecycle, "remove_worktree", _fake_remove)

    first_done = work_done_sync(
        WorkDoneInput(work_id=item_a.name, project_root=project_root.as_posix())
    )
    second_done = work_done_sync(
        WorkDoneInput(work_id=item_b.name, project_root=project_root.as_posix())
    )

    assert "still referenced by work item(s): managed-peer-b" in (first_done.warning or "")
    assert "still referenced by work item(s): managed-peer-a" not in (second_done.warning or "")
    assert "Removed worktree at" in (second_done.warning or "")
    assert len(remove_calls) == 1
    assert remove_calls[0][1] == shared_path.resolve()


def test_shared_managed_worktree_rename_skips_move(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item_a = work_store.create_work_item(project_state_dir, "rename-a", "", None)
    item_b = work_store.create_work_item(project_state_dir, "rename-b", "", None)
    shared_path = tmp_path / "shared-rename"
    shared_path.mkdir(parents=True, exist_ok=True)

    for item in (item_a, item_b):
        work_store.update_work_item_worktree(
            project_state_dir,
            item.name,
            path=shared_path.as_posix(),
            branch=f"feature/{item.name}",
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


def test_work_worktree_without_active_item_shows_guidance(tmp_path: Path) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    output = work_worktree_sync(
        WorkWorktreeInput(
            ensure=False,
            chat_id="chat-without-active-work",
            project_root=project_root.as_posix(),
        )
    )
    assert "No active work item and no tracked temporary worktree" in output.message


def test_work_worktree_missing_manual_assignment_fails_with_guidance(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "manual-missing", "", None)
    manual_path = tmp_path / "manual-missing-path"
    manual_path.mkdir(parents=True, exist_ok=True)
    work_set_worktree_sync(
        WorkSetWorktreeInput(
            work_id=item.name,
            path=manual_path.as_posix(),
            project_root=project_root.as_posix(),
        )
    )
    manual_path.rmdir()

    with pytest.raises(ValueError, match="manual worktree assignment that is missing"):
        work_worktree_sync(
            WorkWorktreeInput(
                work_id=item.name,
                ensure=True,
                project_root=project_root.as_posix(),
            )
        )


def test_work_start_worktree_routes_repo_into_ensure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, _project_state_dir = _setup_project(tmp_path)
    captured: dict[str, object] = {}

    def _fake_ensure(
        *,
        project_root: Path,
        project_state_dir: Path,
        work_id: str,
        target_repo: str | None,
        execution_cwd: Path | None = None,
        dry_run: bool = False,
    ) -> object:
        captured["project_root"] = project_root
        captured["project_state_dir"] = project_state_dir
        captured["work_id"] = work_id
        captured["target_repo"] = target_repo
        captured["execution_cwd"] = execution_cwd
        captured["dry_run"] = dry_run
        return type(
            "_Ensured",
            (),
            {
                "ensured": True,
                "warning": None,
                "worktree_path": project_root / ".dummy",
            },
        )()

    monkeypatch.setattr("meridian.lib.ops.work_lifecycle.ensure_work_item_worktree", _fake_ensure)

    output = work_start_sync(
        WorkStartInput(
            label="repo-routed",
            description="",
            worktree=True,
            repo="../mars-agents",
            project_root=project_root.as_posix(),
        )
    )
    assert output.name == "repo-routed"
    assert captured["work_id"] == "repo-routed"
    assert captured["target_repo"] == "../mars-agents"
    assert captured["dry_run"] is False
