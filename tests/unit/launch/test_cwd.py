from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.launch.cwd import LaunchDirectoryContext, resolve_task_cwd
from meridian.lib.state import work_store


def test_resolve_task_cwd_explicit_work_with_worktree_auto_selects_task_cwd(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "authority"
    state_dir = tmp_path / "state"
    authority_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    work = work_store.create_work_item(state_dir, "feature-work", "", None)
    worktree_path = tmp_path / "feature-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(state_dir, work.name, path=worktree_path.as_posix())

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        explicit_work_id=work.name,
    )

    assert resolved.task_cwd == worktree_path.resolve()
    assert resolved.source == "explicit-work-worktree"
    assert resolved.work_item == work.name


def test_resolve_task_cwd_explicit_work_is_hard_boundary(tmp_path: Path) -> None:
    authority_root = tmp_path / "authority"
    state_dir = tmp_path / "state"
    authority_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    explicit = work_store.create_work_item(state_dir, "explicit-work", "", None)
    ambient = work_store.create_work_item(state_dir, "ambient-work", "", None)
    ambient_path = tmp_path / "ambient-worktree"
    ambient_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(state_dir, ambient.name, path=ambient_path.as_posix())

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        explicit_work_id=explicit.name,
        ambient_work_id=ambient.name,
    )

    assert resolved.task_cwd == authority_root.resolve()
    assert resolved.source == "authority-root"
    assert resolved.work_item is None


def test_resolve_task_cwd_force_worktree_uses_ambient_work_item(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "authority"
    state_dir = tmp_path / "state"
    authority_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    ambient = work_store.create_work_item(state_dir, "ambient-work", "", None)
    ambient_path = tmp_path / "ambient-worktree"
    ambient_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(state_dir, ambient.name, path=ambient_path.as_posix())

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        ambient_work_id=ambient.name,
        force_worktree=True,
    )

    assert resolved.task_cwd == ambient_path.resolve()
    assert resolved.source == "forced-worktree"
    assert resolved.work_item == ambient.name


def test_resolve_task_cwd_force_worktree_requires_selected_work_item(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "authority"
    state_dir = tmp_path / "state"
    authority_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="--worktree requires a selected work item"):
        resolve_task_cwd(
            authority_root,
            project_state_dir=state_dir,
            force_worktree=True,
        )


def test_resolve_task_cwd_force_worktree_requires_configured_worktree(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "authority"
    state_dir = tmp_path / "state"
    authority_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    work = work_store.create_work_item(state_dir, "no-worktree", "", None)

    with pytest.raises(ValueError, match="has no configured worktree path"):
        resolve_task_cwd(
            authority_root,
            project_state_dir=state_dir,
            explicit_work_id=work.name,
            force_worktree=True,
        )


def test_resolve_task_cwd_stale_worktree_path_is_hard_error_unless_no_worktree(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "authority"
    state_dir = tmp_path / "state"
    authority_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    work = work_store.create_work_item(state_dir, "broken-work", "", None)
    missing_path = tmp_path / "missing-worktree"
    work_store.update_work_item_worktree(state_dir, work.name, path=missing_path.as_posix())

    with pytest.raises(ValueError):
        resolve_task_cwd(authority_root, project_state_dir=state_dir, explicit_work_id=work.name)

    bypassed = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        explicit_work_id=work.name,
        force_no_worktree=True,
    )
    assert bypassed.task_cwd == authority_root.resolve()
    assert bypassed.source == "forced-no-worktree"


def test_launch_directory_context_requires_instruction_when_process_cwd_differs(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "authority"
    authority_root.mkdir(parents=True, exist_ok=True)
    task_cwd = tmp_path / "task"
    task_cwd.mkdir(parents=True, exist_ok=True)
    aligned_context = LaunchDirectoryContext(
        authority_root=authority_root.resolve(),
        logical_task_cwd=task_cwd.resolve(),
        reference_anchor=task_cwd.resolve(),
        actual_process_cwd=task_cwd.resolve(),
        task_cwd_source="explicit-work-worktree",
        work_item="feature-work",
    )
    forced_context = LaunchDirectoryContext(
        authority_root=authority_root.resolve(),
        logical_task_cwd=task_cwd.resolve(),
        reference_anchor=task_cwd.resolve(),
        actual_process_cwd=authority_root.resolve(),
        task_cwd_source="explicit-work-worktree",
        work_item="feature-work",
    )

    assert aligned_context.requires_task_cwd_instruction is False
    assert forced_context.requires_task_cwd_instruction is True


def test_launch_directory_context_from_task_cwd_resolution_sets_reference_anchor(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "authority"
    state_dir = tmp_path / "state"
    authority_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    work = work_store.create_work_item(state_dir, "feature-work", "", None)
    worktree_path = tmp_path / "feature-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(state_dir, work.name, path=worktree_path.as_posix())
    resolution = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        explicit_work_id=work.name,
    )

    context = LaunchDirectoryContext.from_task_cwd_resolution(
        authority_root=authority_root,
        task_cwd_resolution=resolution,
    )

    assert context.authority_root == authority_root.resolve()
    assert context.logical_task_cwd == worktree_path.resolve()
    assert context.reference_anchor == worktree_path.resolve()
    assert context.actual_process_cwd == worktree_path.resolve()
    assert context.task_cwd_source == "explicit-work-worktree"
    assert context.work_item == work.name
