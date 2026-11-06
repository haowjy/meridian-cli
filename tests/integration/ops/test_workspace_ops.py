from pathlib import Path

import pytest

from meridian.lib.ops.workspace import WorkspaceInitInput, workspace_init_sync


@pytest.fixture(autouse=True)
def _clear_state_root_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("_MERIDIAN_RUNTIME_DIR", raising=False)


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text("", encoding="utf-8")
    return project_root


def test_workspace_init_appends_scaffold_to_existing_local_config_without_workspace(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / ".git").mkdir()
    local_path = project_root / "meridian.local.toml"
    local_path.write_text('model = "gpt-5"\n', encoding="utf-8")

    first = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))
    second = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))

    content = local_path.read_text(encoding="utf-8")
    assert first.created is True
    assert second.created is False
    assert 'model = "gpt-5"' in content
    assert "Workspace topology — local path overrides and additions." in content
    assert content.count("[workspace.example]") == 1


def test_workspace_init_recognizes_existing_commented_scaffold_without_duplication(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / ".git").mkdir()
    local_path = project_root / "meridian.local.toml"
    existing = (
        "# Local overrides\n"
        "# Workspace topology — local path overrides and additions.\n"
        "# [workspace.example]\n"
        '# path = "../sibling-repo"\n'
    )
    local_path.write_text(existing, encoding="utf-8")

    first = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))
    second = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))

    assert first.created is False
    assert second.created is False
    assert local_path.read_text(encoding="utf-8") == existing
    assert local_path.read_text(encoding="utf-8").count("# [workspace.example]") == 1
