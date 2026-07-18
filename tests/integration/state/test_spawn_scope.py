from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn_scope import (
    SpawnScope,
    read_spawn_scope,
    write_spawn_scope_task_dir,
)

pytestmark = pytest.mark.slow


def _project(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    resolve_project_runtime_root_for_write(project_root)
    return project_root


def test_spawn_scope_write_read_round_trip(tmp_path: Path) -> None:
    project_root = _project(tmp_path)
    task_dir = tmp_path / "worktree"
    task_dir.mkdir(parents=True)

    write_spawn_scope_task_dir(project_root, "p1", task_dir)
    scope = read_spawn_scope(project_root, "p1")

    assert scope == SpawnScope(task_dir=task_dir.resolve(), task_dir_cleared=False)


def test_spawn_scope_tombstone_sets_cleared_flag(tmp_path: Path) -> None:
    project_root = _project(tmp_path)

    write_spawn_scope_task_dir(project_root, "p1", None)
    scope = read_spawn_scope(project_root, "p1")

    assert scope.task_dir is None
    assert scope.task_dir_cleared is True


def test_spawn_scope_missing_file_is_absent_not_cleared(tmp_path: Path) -> None:
    project_root = _project(tmp_path)

    scope = read_spawn_scope(project_root, "p-missing")

    assert scope == SpawnScope()


def test_spawn_scope_corrupt_file_falls_through(tmp_path: Path) -> None:
    project_root = _project(tmp_path)
    scope_dir = resolve_project_runtime_root_for_write(project_root) / "spawns" / "p1"
    scope_dir.mkdir(parents=True)
    (scope_dir / "scope.json").write_text("{not json", encoding="utf-8")

    scope = read_spawn_scope(project_root, "p1")

    assert scope == SpawnScope()


def test_spawn_scope_empty_file_falls_through(tmp_path: Path) -> None:
    project_root = _project(tmp_path)
    scope_dir = resolve_project_runtime_root_for_write(project_root) / "spawns" / "p1"
    scope_dir.mkdir(parents=True)
    (scope_dir / "scope.json").write_text("", encoding="utf-8")

    scope = read_spawn_scope(project_root, "p1")

    assert scope == SpawnScope()


def test_write_spawn_scope_uses_runtime_write_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-meridian"
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    task_dir = tmp_path / "worktree"
    task_dir.mkdir(parents=True)

    write_spawn_scope_task_dir(project_root, "p1", task_dir)

    repo_scope = project_root / ".meridian" / "spawns" / "p1" / "scope.json"
    write_scope = (
        resolve_project_runtime_root_for_write(project_root) / "spawns" / "p1" / "scope.json"
    )
    assert not repo_scope.exists()
    assert write_scope.is_file()
