from pathlib import Path

import pytest

from meridian.lib.config.project_paths import (
    PROJECT_ROOT_IGNORE_TARGETS,
    resolve_project_config_paths,
)


@pytest.fixture(autouse=True)
def _clear_state_root_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
def test_resolve_project_paths_resolves_project_root_and_execution_cwd(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    execution_cwd = tmp_path / "exec"
    project_root.mkdir()
    execution_cwd.mkdir()

    paths = resolve_project_config_paths(project_root=project_root, execution_cwd=execution_cwd)

    assert paths.project_root == project_root.resolve()
    assert paths.execution_cwd == execution_cwd.resolve()
def test_project_paths_exposes_root_file_policy(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    paths = resolve_project_config_paths(project_root=project_root)

    assert paths.meridian_toml == project_root.resolve() / "meridian.toml"
    assert paths.meridian_local_toml == project_root.resolve() / "meridian.local.toml"
    assert paths.workspace_ignore_targets == PROJECT_ROOT_IGNORE_TARGETS
