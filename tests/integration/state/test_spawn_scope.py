from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn_scope import (
    SpawnScope,
    read_spawn_scope,
    write_spawn_scope_task_dir,
)
from meridian.lib.state.spawn_store import start_spawn

pytestmark = pytest.mark.slow


def _project(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    resolve_project_runtime_root_for_write(project_root)
    return project_root


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
    "content", ["{not json", ""], ids=["corrupt-json", "empty-file"]
)
def test_spawn_scope_malformed_file_falls_through(tmp_path: Path, content: str) -> None:
    project_root = _project(tmp_path)
    scope_dir = resolve_project_runtime_root_for_write(project_root) / "spawns" / "p1"
    scope_dir.mkdir(parents=True)
    (scope_dir / "scope.json").write_text(content, encoding="utf-8")

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
    _publish_spawn(project_root)

    write_spawn_scope_task_dir(project_root, "p1", task_dir)

    repo_scope = project_root / ".meridian" / "spawns" / "p1" / "scope.json"
    write_scope = (
        resolve_project_runtime_root_for_write(project_root) / "spawns" / "p1" / "scope.json"
    )
    assert not repo_scope.exists()
    assert write_scope.is_file()
