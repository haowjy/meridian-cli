from pathlib import Path

import pytest

from meridian.lib.ops.workspace import (
    WorkspaceInitInput,
    WorkspaceMigrateInput,
    workspace_init_sync,
    workspace_migrate_sync,
)


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
    assert "workspace.local.toml" in exclude_lines
    assert exclude_lines.count("workspace.local.toml") == 1
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
        '# Local overrides\n'
        '# Workspace topology — local path overrides and additions.\n'
        '# [workspace.example]\n'
        '# path = "../sibling-repo"\n'
    )
    local_path.write_text(existing, encoding="utf-8")

    first = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))
    second = workspace_init_sync(WorkspaceInitInput(project_root=project_root.as_posix()))

    assert first.created is False
    assert second.created is False
    assert local_path.read_text(encoding="utf-8") == existing
    assert local_path.read_text(encoding="utf-8").count("# [workspace.example]") == 1


@pytest.mark.parametrize(
    ("setup_kind", "target_name", "expected_message"),
    [
        ("missing", "workspace.local.toml", "does not exist"),
        ("legacy_dir", "workspace.local.toml", "exists but is not a file"),
        ("local_dir", "meridian.local.toml", "exists but is not a file"),
    ],
    ids=["missing_legacy", "non_file_legacy", "non_file_destination"],
)
def test_workspace_migrate_file_shape_errors(
    tmp_path: Path,
    setup_kind: str,
    target_name: str,
    expected_message: str,
) -> None:
    project_root = _repo(tmp_path)

    if setup_kind in {"legacy_dir", "local_dir"}:
        (project_root / target_name).mkdir()
    if setup_kind == "local_dir":
        (project_root / "workspace.local.toml").write_text(
            '[[context-roots]]\npath = "../new-web"\n',
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match=expected_message):
        workspace_migrate_sync(WorkspaceMigrateInput(project_root=project_root.as_posix()))


def test_workspace_migrate_clean_migration(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "workspace.local.toml").write_text(
        '[[context-roots]]\npath = "../meridian-web"\n\n'
        '[[context-roots]]\npath = "../prompts/meridian-base"\n',
        encoding="utf-8",
    )

    result = workspace_migrate_sync(WorkspaceMigrateInput(project_root=project_root.as_posix()))

    local_path = project_root / "meridian.local.toml"
    content = local_path.read_text(encoding="utf-8")
    assert result.path == local_path.as_posix()
    assert result.migrated_entries == 2
    assert [(entry.name, entry.original_path) for entry in result.entries] == [
        ("meridian-web", "../meridian-web"),
        ("meridian-base", "../prompts/meridian-base"),
    ]
    assert "# Auto-migrated from workspace.local.toml" in content
    assert '[workspace.meridian-web]\npath = "../meridian-web"' in content


def test_workspace_migrate_force_overwrites_existing_workspace(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "workspace.local.toml").write_text(
        '[[context-roots]]\npath = "../new-web"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        'model = "gpt"\n\n[workspace.old]\npath = "../old"\n',
        encoding="utf-8",
    )

    workspace_migrate_sync(
        WorkspaceMigrateInput(project_root=project_root.as_posix(), force=True)
    )

    content = (project_root / "meridian.local.toml").read_text(encoding="utf-8")
    assert 'model = "gpt"' in content
    assert "[workspace.old]" not in content
    assert "[workspace.new-web]" in content


def test_workspace_migrate_force_preserves_array_tables_after_workspace(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "workspace.local.toml").write_text(
        '[[context-roots]]\npath = "../new-web"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        'model = "gpt"\n\n'
        "[workspace.old]\n"
        'path = "../old"\n\n'
        "[[hooks]]\n"
        'command = "echo kept"\n',
        encoding="utf-8",
    )

    workspace_migrate_sync(
        WorkspaceMigrateInput(project_root=project_root.as_posix(), force=True)
    )

    content = (project_root / "meridian.local.toml").read_text(encoding="utf-8")
    assert "[workspace.old]" not in content
    assert "[[hooks]]" in content
    assert 'command = "echo kept"' in content
    assert "[workspace.new-web]" in content


def test_workspace_migrate_pre_existing_workspace_aborts(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "workspace.local.toml").write_text(
        '[[context-roots]]\npath = "../new-web"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        '[workspace.old]\npath = "../old"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="already exists"):
        workspace_migrate_sync(WorkspaceMigrateInput(project_root=project_root.as_posix()))


def test_workspace_migrate_basename_collisions(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "workspace.local.toml").write_text(
        '[[context-roots]]\npath = "../web"\n\n'
        '[[context-roots]]\npath = "../Web"\n\n'
        '[[context-roots]]\npath = "../web!"\n',
        encoding="utf-8",
    )

    result = workspace_migrate_sync(WorkspaceMigrateInput(project_root=project_root.as_posix()))

    assert [entry.name for entry in result.entries] == ["web", "web-2", "web-3"]


def test_workspace_migrate_unsafe_basenames(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "workspace.local.toml").write_text(
        '[[context-roots]]\npath = "."\n\n'
        '[[context-roots]]\npath = ".."\n\n'
        '[[context-roots]]\npath = "/"\n',
        encoding="utf-8",
    )

    result = workspace_migrate_sync(WorkspaceMigrateInput(project_root=project_root.as_posix()))

    assert [entry.name for entry in result.entries] == ["root-1", "root-2", "root-3"]


def test_workspace_migrate_preserves_paths_verbatim(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    original_path = "../Some Dir/Repo!"
    (project_root / "workspace.local.toml").write_text(
        f'[[context-roots]]\npath = "{original_path}"\n',
        encoding="utf-8",
    )

    result = workspace_migrate_sync(WorkspaceMigrateInput(project_root=project_root.as_posix()))

    content = (project_root / "meridian.local.toml").read_text(encoding="utf-8")
    assert result.entries[0].original_path == original_path
    assert f'path = "{original_path}"' in content


def test_workspace_migrate_skips_disabled_legacy_roots_with_warning(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "workspace.local.toml").write_text(
        '[[context-roots]]\npath = "../enabled"\n\n'
        '[[context-roots]]\npath = "../disabled"\nenabled = false\n',
        encoding="utf-8",
    )

    result = workspace_migrate_sync(WorkspaceMigrateInput(project_root=project_root.as_posix()))

    content = (project_root / "meridian.local.toml").read_text(encoding="utf-8")
    assert result.migrated_entries == 1
    assert [(entry.name, entry.original_path) for entry in result.entries] == [
        ("enabled", "../enabled"),
    ]
    assert "[workspace.enabled]" in content
    assert "../disabled" not in content
    assert any("Skipped 1 disabled legacy root(s)" in warning for warning in result.warnings)


def test_workspace_migrate_advisory_text(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "workspace.local.toml").write_text(
        '[[context-roots]]\npath = "../web"\n',
        encoding="utf-8",
    )

    result = workspace_migrate_sync(WorkspaceMigrateInput(project_root=project_root.as_posix()))
    formatted = result.format_text()

    assert "Review and rename auto-generated workspace entry names" in formatted
    assert "canonical names chosen by the project" in formatted
