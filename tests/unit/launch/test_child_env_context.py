"""ChildEnvContext ambient work-dir resolution at the shared child-env seam."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.launch.context import ChildEnvContext, build_child_runtime_env_overrides
from meridian.lib.state.paths import resolve_ambient_work_dir


def _project_paths(project_root: Path) -> ProjectConfigPaths:
    return ProjectConfigPaths(
        project_root=project_root,
        execution_cwd=project_root,
    )


def test_child_env_context_resolves_child_ambient_not_parent_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / "meridian.toml").write_text(
        '[project]\nid = "proj-child-env"\n', encoding="utf-8"
    )
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    parent_ambient = tmp_path / "parent-ambient"

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", parent_ambient.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)

    ctx = ChildEnvContext.from_environment(
        project_paths=_project_paths(project_root),
        runtime_root=runtime_root,
    )

    assert ctx.work_id is None
    assert ctx.work_dir is None

    expected_child = resolve_ambient_work_dir(
        project_root, "p-child", runtime_root=runtime_root
    )
    overrides = ctx.child_context(child_spawn_id="p-child")
    assert overrides["MERIDIAN_ACTIVE_WORK_DIR"] == expected_child.as_posix()
    assert overrides["MERIDIAN_ACTIVE_WORK_DIR"] != parent_ambient.as_posix()
    assert "MERIDIAN_ACTIVE_WORK_ID" not in overrides


def test_build_child_runtime_env_overrides_named_work_uses_scratch_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / "meridian.toml").write_text(
        '[project]\nid = "proj-named-work"\n', encoding="utf-8"
    )
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)

    work_id = "feature-x"
    overrides = build_child_runtime_env_overrides(
        project_paths=_project_paths(project_root),
        runtime_root=runtime_root,
        child_spawn_id="p-child",
        work_id=work_id,
    )

    assert overrides["MERIDIAN_ACTIVE_WORK_ID"] == work_id
    assert overrides["MERIDIAN_ACTIVE_WORK_DIR"]
    assert work_id in overrides["MERIDIAN_ACTIVE_WORK_DIR"]
