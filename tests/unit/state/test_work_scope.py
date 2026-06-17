from __future__ import annotations

from pathlib import Path

from meridian.lib.state.paths import resolve_ambient_work_dir
from meridian.lib.state.work_scope import resolve_bound_work_scope


def test_resolve_bound_work_scope_named_work_item(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    (project_root / ".meridian" / "id").parent.mkdir(parents=True)
    (project_root / ".meridian" / "id").write_text("proj-bind-named", encoding="utf-8")

    scope = resolve_bound_work_scope(
        project_root=project_root,
        requested_work_id="feature-a",
        child_spawn_id="p9",
    )

    assert scope.kind == "work_item"
    assert scope.identifier == "feature-a"
    assert scope.is_durable
    assert scope.root.name == "feature-a"


def test_resolve_bound_work_scope_ambient_spawn(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    (project_root / ".meridian" / "id").parent.mkdir(parents=True)
    (project_root / ".meridian" / "id").write_text("proj-bind-ambient", encoding="utf-8")

    scope = resolve_bound_work_scope(
        project_root=project_root,
        requested_work_id=None,
        child_spawn_id="p12",
    )

    expected = resolve_ambient_work_dir(project_root, "p12")
    assert scope.kind == "ambient_spawn"
    assert scope.identifier == "p12"
    assert scope.is_ephemeral
    assert scope.root == expected
