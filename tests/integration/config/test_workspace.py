from pathlib import Path

import pytest

from meridian.lib.config.workspace import resolve_workspace_snapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)
from tests.support.launch import build_launch_context


@pytest.fixture(autouse=True)
def _clear_state_root_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("_MERIDIAN_RUNTIME_DIR", raising=False)


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def _build_workspace_launch_context(
    *,
    project_root: Path,
    surface: LaunchCompositionSurface = LaunchCompositionSurface.DIRECT,
):
    request = SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt="hello",
    )
    runtime = LaunchRuntime(
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
        composition_surface=surface,
        runtime_root=(project_root / ".meridian").as_posix(),
        project_paths_project_root=project_root.as_posix(),
        project_paths_execution_cwd=project_root.as_posix(),
    )
    return build_launch_context(
        spawn_id="p-workspace",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )


def test_user_global_workspace_config_is_ignored_for_snapshot_and_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    user_workspace_root = tmp_path / "user-workspace-root"
    user_workspace_root.mkdir()
    user_config_path = tmp_path / "user-config.toml"
    user_config_path.write_text(
        f'[workspace.user_docs]\npath = "{user_workspace_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config_path.as_posix())

    snapshot = resolve_workspace_snapshot(project_root)
    runtime_ctx = _build_workspace_launch_context(project_root=project_root)

    assert snapshot.status == "none"
    assert snapshot.source_paths == ()
    assert snapshot.roots == ()
    assert snapshot.findings == ()
    bind_overrides = runtime_ctx.binding.environment.bind_env_overrides
    assert user_workspace_root.as_posix() not in bind_overrides.values()
    assert all(
        user_workspace_root.as_posix() not in arg
        for arg in runtime_ctx.binding.run_params.extra_args
    )
