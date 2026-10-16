from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.launch.cwd import (
    EffectiveTaskDir,
    resolve_effective_task_dir,
    resolve_task_cwd,
)
from meridian.lib.state import work_repository
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn_scope import write_spawn_scope_task_dir
from meridian.lib.state.spawn_store import start_spawn


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    authority_root = tmp_path / "authority"
    state_dir = tmp_path / "state"
    authority_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return authority_root, state_dir


def _work_with_task_dir(state_dir: Path, tmp_path: Path, name: str) -> str:
    item = work_repository.create_work_item(state_dir, name, "", None)
    task_dir = tmp_path / f"{name}-task-dir"
    task_dir.mkdir(parents=True, exist_ok=True)
    work_repository.update_work_item_task_dir(state_dir, item.name, task_dir=task_dir.as_posix())
    return item.name


def _work_with_stale_task_dir(state_dir: Path, tmp_path: Path, name: str) -> tuple[str, Path]:
    item = work_repository.create_work_item(state_dir, name, "", None)
    stale = tmp_path / f"{name}-removed-task-dir"
    work_repository.update_work_item_task_dir(state_dir, item.name, task_dir=stale.as_posix())
    return item.name, stale.resolve()


def _publish_spawn(project_root: Path, spawn_id: str = "p1") -> None:
    start_spawn(
        resolve_project_runtime_root_for_write(project_root),
        chat_id="c1",
        model="gpt-5.6",
        agent="coder",
        harness="codex",
        prompt="test",
        spawn_id=spawn_id,
    )


@pytest.mark.parametrize(
    (
        "explicit_task",
        "explicit_work",
        "ambient_work",
        "inherited_task",
        "caller_cwd",
        "expected_path",
        "expected_source",
        "expected_work",
        "expected_warning",
        "error",
    ),
    [
        (
            "explicit",
            None,
            "stale",
            None,
            None,
            "explicit",
            "explicit-task-dir",
            "skip",
            False,
            None,
        ),
        (
            "missing",
            None,
            None,
            None,
            None,
            None,
            None,
            "skip",
            None,
            "task_dir does not exist",
        ),
        (
            None,
            "empty",
            "ambient",
            None,
            None,
            "authority",
            "explicit-work-authority-root",
            "empty",
            None,
            None,
        ),
        (
            None,
            None,
            "stale",
            None,
            None,
            "authority",
            "ambient-work-authority-root",
            "stale",
            True,
            None,
        ),
        (None, None, None, None, "outside", "outside", "ambient-cwd", "none", None, None),
        (None, None, None, None, "inside", "authority", "authority-root", "skip", None, None),
        (
            "explicit",
            None,
            None,
            None,
            "outside",
            "explicit",
            "explicit-task-dir",
            "skip",
            None,
            None,
        ),
        (
            None,
            None,
            "feature",
            None,
            "outside",
            "feature",
            "ambient-work-task-dir",
            "skip",
            None,
            None,
        ),
        (
            None,
            None,
            "ambient",
            "inherited",
            None,
            "inherited",
            "inherited-task-dir",
            "skip",
            None,
            None,
        ),
        (
            None,
            "feature",
            None,
            "inherited",
            None,
            "feature",
            "explicit-work-task-dir",
            "skip",
            None,
            None,
        ),
        (
            None,
            "empty",
            None,
            "inherited",
            None,
            "authority",
            "explicit-work-authority-root",
            "skip",
            None,
            None,
        ),
        (
            None,
            None,
            None,
            "inside-worktree",
            "inside-worktree",
            "inside-worktree",
            "inherited-task-dir",
            "skip",
            None,
            None,
        ),
    ],
    ids=(
        "explicit-over-stale-work",
        "missing-explicit",
        "explicit-work-boundary",
        "stale-ambient-work",
        "outside-caller",
        "inside-caller",
        "explicit-over-caller",
        "work-over-caller",
        "inherited-over-ambient",
        "explicit-work-over-inherited",
        "empty-explicit-work-over-inherited",
        "inherited-inside-project",
    ),
)
def test_resolve_task_cwd_precedence(
    tmp_path: Path,
    explicit_task: str | None,
    explicit_work: str | None,
    ambient_work: str | None,
    inherited_task: str | None,
    caller_cwd: str | None,
    expected_path: str | None,
    expected_source: str | None,
    expected_work: str,
    expected_warning: bool | None,
    error: str | None,
) -> None:
    authority_root, state_dir = _roots(tmp_path)
    paths = {
        "authority": authority_root,
        "explicit": tmp_path / "explicit-task",
        "missing": tmp_path / "missing-task-dir",
        "outside": tmp_path / "worktree",
        "inside": authority_root / "src" / "meridian",
        "inherited": tmp_path / "inherited-task",
        "inside-worktree": authority_root / "worktree",
        "feature": tmp_path / "feature-work-task-dir",
    }
    for name, path in paths.items():
        if name != "missing":
            path.mkdir(parents=True, exist_ok=True)

    works = {
        "empty": work_repository.create_work_item(state_dir, "explicit-work", "", None).name,
        "ambient": _work_with_task_dir(state_dir, tmp_path, "ambient-work"),
        "feature": _work_with_task_dir(state_dir, tmp_path, "feature-work"),
    }
    works["stale"], stale_path = _work_with_stale_task_dir(state_dir, tmp_path, "stale-work")
    kwargs = {
        "explicit_task_dir": paths[explicit_task].as_posix() if explicit_task else None,
        "explicit_work_id": works[explicit_work] if explicit_work else None,
        "ambient_work_id": works[ambient_work] if ambient_work else None,
        "inherited_task_dir": paths[inherited_task].as_posix() if inherited_task else None,
        "caller_cwd": paths[caller_cwd].as_posix() if caller_cwd else None,
    }

    if error is not None:
        with pytest.raises(ValueError, match=error):
            resolve_task_cwd(authority_root, project_state_dir=state_dir, **kwargs)
        return

    resolved = resolve_task_cwd(authority_root, project_state_dir=state_dir, **kwargs)

    assert resolved.task_cwd == paths[expected_path].resolve()
    assert resolved.source == expected_source
    if expected_work != "skip":
        assert resolved.work_item == (None if expected_work == "none" else works[expected_work])
    if expected_warning is not None:
        assert (resolved.warning is not None) is expected_warning
    if expected_warning:
        assert stale_path.as_posix() in resolved.warning


def test_resolve_effective_task_dir_precedence_scope_work_inherited_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True)
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    inherited = tmp_path / "inherited"
    inherited.mkdir(parents=True)
    scope_dir = tmp_path / "scope-task"
    scope_dir.mkdir(parents=True)
    work_task_dir = tmp_path / "work-task"
    work_task_dir.mkdir(parents=True)
    work_id = _work_with_task_dir(state_dir, tmp_path, "feature")
    _publish_spawn(project_root)

    write_spawn_scope_task_dir(project_root, "p1", scope_dir)
    effective = resolve_effective_task_dir(
        project_root=project_root,
        project_state_dir=state_dir,
        spawn_id="p1",
        inherited_task_dir=inherited,
        work_id=work_id,
    )
    assert effective == EffectiveTaskDir(task_dir=scope_dir.resolve(), source="scope")

    write_spawn_scope_task_dir(project_root, "p1", None)
    effective = resolve_effective_task_dir(
        project_root=project_root,
        project_state_dir=state_dir,
        spawn_id="p1",
        inherited_task_dir=inherited,
        work_id=work_id,
    )
    assert effective == EffectiveTaskDir(
        task_dir=(tmp_path / "feature-task-dir").resolve(),
        source="work-item",
    )

    effective = resolve_effective_task_dir(
        project_root=project_root,
        project_state_dir=state_dir,
        spawn_id=None,
        inherited_task_dir=inherited,
        work_id=None,
    )
    assert effective == EffectiveTaskDir(task_dir=inherited.resolve(), source="inherited")

    effective = resolve_effective_task_dir(
        project_root=project_root,
        project_state_dir=state_dir,
        spawn_id=None,
        inherited_task_dir=None,
        work_id=None,
    )
    assert effective == EffectiveTaskDir(
        task_dir=project_root.resolve(),
        source="project-root",
    )


def test_resolve_effective_task_dir_tombstone_skips_inherited(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True)
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    inherited = tmp_path / "inherited"
    inherited.mkdir(parents=True)
    _publish_spawn(project_root)

    write_spawn_scope_task_dir(project_root, "p1", None)
    effective = resolve_effective_task_dir(
        project_root=project_root,
        project_state_dir=state_dir,
        spawn_id="p1",
        inherited_task_dir=inherited,
        work_id=None,
    )

    assert effective == EffectiveTaskDir(
        task_dir=project_root.resolve(),
        source="project-root",
    )


def test_resolve_effective_task_dir_stale_work_falls_back_with_warning(tmp_path: Path) -> None:
    project_root, state_dir = _roots(tmp_path)
    work_id, stale = _work_with_stale_task_dir(state_dir, tmp_path, "stale-work")

    effective = resolve_effective_task_dir(
        project_root=project_root,
        project_state_dir=state_dir,
        work_id=work_id,
    )

    assert effective.task_dir == project_root.resolve()
    assert effective.source == "project-root"
    assert effective.warning is not None
    assert stale.as_posix() in effective.warning
