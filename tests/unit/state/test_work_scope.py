from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.state.paths import resolve_ambient_work_dir
from meridian.lib.state.work_scope import (
    WorkScope,
    resolve_bound_work_scope,
    resolve_work_scope_from_parts,
)


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


def test_from_environment_populates_work_scope_kind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-scope-kind", encoding="utf-8")

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p42")
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)

    ctx = ResolvedContext.from_environment()

    assert ctx.work_scope is not None
    assert ctx.work_scope.kind == "ambient_spawn"
    assert ctx.work_scope.identifier == "p42"
    assert ctx.work_dir == ctx.work_scope.root


def test_from_environment_named_work_scope_is_durable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-scope-named", encoding="utf-8")

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "my-feature")

    ctx = ResolvedContext.from_environment()

    assert ctx.work_scope is not None
    assert ctx.work_scope.kind == "work_item"
    assert ctx.work_scope.identifier == "my-feature"
    assert ctx.work_scope.is_durable


def test_resolve_work_scope_from_parts_prefers_bound_dir_with_work_id() -> None:
    bound = Path("/tmp/custom-bound")
    scope = resolve_work_scope_from_parts(
        project_root=Path("/repo"),
        runtime_root=Path("/runtime"),
        spawn_id=None,
        work_id="attached",
        bound_work_dir=bound,
    )

    assert scope == WorkScope(kind="work_item", identifier="attached", root=bound)


def test_resolve_work_scope_from_parts_session_named_dir(
    tmp_path: Path,
) -> None:
    backend = MagicMock()

    def _resolve_scratch_dir(root: Path, work_id: str) -> Path:
        return root / "work" / work_id

    backend.resolve_work_scratch_dir.side_effect = _resolve_scratch_dir
    backend.get_session_active_work_id.return_value = "session-work"

    ctx = ResolvedContext.from_environment(
        explicit_project_root=tmp_path / "repo",
        explicit_runtime_root=tmp_path / "runtime",
        explicit_chat_id="target-chat",
        backend=backend,
    )

    assert ctx.work_scope is not None
    assert ctx.work_scope.kind == "work_item"
    assert ctx.work_scope.identifier == "session-work"
