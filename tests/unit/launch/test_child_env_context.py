"""ChildEnvContext dir-without-id propagation."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.launch.context import ChildEnvContext


def test_child_env_context_inherits_dir_without_work_id(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-child-env", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    ambient_dir = tmp_path / "parent-ambient"

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", ambient_dir.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)

    project_paths = ProjectConfigPaths(
        project_root=project_root,
        execution_cwd=project_root,
    )
    ctx = ChildEnvContext.from_environment(
        project_paths=project_paths,
        runtime_root=runtime_root,
    )

    assert ctx.work_id is None
    assert ctx.work_dir == ambient_dir.resolve()

    overrides = ctx.child_context(child_spawn_id="p-child")
    assert overrides["MERIDIAN_ACTIVE_WORK_DIR"] == ambient_dir.as_posix()
    assert "MERIDIAN_ACTIVE_WORK_ID" not in overrides
