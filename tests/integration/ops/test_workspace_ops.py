from pathlib import Path

import pytest

from meridian.lib.ops.workspace import WorkspaceInitInput, workspace_init_sync


@pytest.fixture(autouse=True)
def _clear_state_root_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def test_workspace_init_creates_template_and_local_gitignore_entry(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / ".git").mkdir()

    first = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))
    second = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))

    workspace_path = project_root / "meridian.local.toml"
    exclude_path = project_root / ".git" / "info" / "exclude"
    content = workspace_path.read_text(encoding="utf-8")
    exclude_lines = exclude_path.read_text(encoding="utf-8").splitlines()

    assert first.created is True
    assert first.path == workspace_path.as_posix()
    assert first.local_gitignore_path == exclude_path.as_posix()
    assert first.local_gitignore_updated is True
    assert second.created is False
    assert second.local_gitignore_updated is False
    assert "Workspace topology — local path overrides and additions." in content
    assert "[workspace.example]" in content
    assert "meridian.local.toml" in exclude_lines
    assert exclude_lines.count("meridian.local.toml") == 1


def test_workspace_init_resolves_worktree_gitdir_pointer(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    git_dir = tmp_path / "main-git-dir" / "worktrees" / "feature"
    (git_dir / "info").mkdir(parents=True)
    (project_root / ".git").write_text(f"gitdir: {git_dir.as_posix()}\n", encoding="utf-8")

    result = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))

    assert result.local_gitignore_path is None
    assert result.local_gitignore_updated is False
    assert not (git_dir / "info" / "exclude").exists()


def test_workspace_init_handles_missing_git_metadata_without_failure(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    result = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))

    assert result.created is True
    assert result.local_gitignore_path is None
    assert result.local_gitignore_updated is False


def test_workspace_init_rejects_non_file_workspace_target(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.local.toml").mkdir()

    with pytest.raises(ValueError, match="is not a file"):
        workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))


def test_workspace_init_ignores_state_root_parent_for_workspace_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / ".git").mkdir()
    override_root = tmp_path / "state-root" / ".meridian"
    override_root.parent.mkdir(parents=True)
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", override_root.as_posix())

    result = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))

    expected_workspace_path = project_root / "meridian.local.toml"
    assert result.path == expected_workspace_path.as_posix()
    assert expected_workspace_path.is_file()


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
