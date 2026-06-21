from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.state.spawn_scope import (
    SpawnScope,
    read_spawn_scope,
    write_spawn_scope_task_dir,
)


def _project(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / ".meridian").mkdir(parents=True)
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
    scope_dir = project_root / ".meridian" / "spawns" / "p1"
    scope_dir.mkdir(parents=True)
    (scope_dir / "scope.json").write_text("{not json", encoding="utf-8")

    scope = read_spawn_scope(project_root, "p1")

    assert scope == SpawnScope()


def test_spawn_scope_empty_file_falls_through(tmp_path: Path) -> None:
    project_root = _project(tmp_path)
    scope_dir = project_root / ".meridian" / "spawns" / "p1"
    scope_dir.mkdir(parents=True)
    (scope_dir / "scope.json").write_text("", encoding="utf-8")

    scope = read_spawn_scope(project_root, "p1")

    assert scope == SpawnScope()


def test_spawn_scope_tombstone_json_shape(tmp_path: Path) -> None:
    project_root = _project(tmp_path)

    write_spawn_scope_task_dir(project_root, "p1", None)
    scope_path = project_root / ".meridian" / "spawns" / "p1" / "scope.json"

    assert json.loads(scope_path.read_text(encoding="utf-8")) == {"task_dir": None}
