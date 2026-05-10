from pathlib import Path

import pytest

from meridian.lib.config.context_config import ContextConfig
from meridian.lib.state.paths import (
    RuntimePaths,
    ensure_gitignore,
    load_context_config,
    resolve_project_paths,
    resolve_project_paths_for_write,
    resolve_project_paths_from_context,
    resolve_runtime_paths,
    resolve_work_scratch_dir_for_project,
)


def test_ensure_gitignore_preserves_existing_lines_and_adds_required_entries(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    gitignore_path = project_root / ".meridian" / ".gitignore"
    gitignore_path.parent.mkdir(parents=True)
    gitignore_path.write_text(
        "\n".join(
            [
                "# Ignore everything by default",
                "*",
                "!.gitignore",
                "!config.toml",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ensure_gitignore(project_root)

    updated = gitignore_path.read_text(encoding="utf-8")
    assert "!config.toml" in updated
    assert "!.gitignore" in updated
    assert "!id" in updated


def test_resolve_runtime_paths_does_not_expose_project_config_path(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    paths = resolve_runtime_paths(project_root)

    assert not hasattr(paths, "config_path")


def test_state_root_paths_resolves_hook_state_json(tmp_path: Path) -> None:
    runtime_root = tmp_path / "state"
    paths = RuntimePaths.from_root_dir(runtime_root)

    assert paths.hook_state_json == runtime_root / "hook-state.json"


def test_state_root_paths_override_meridian_dir_stays_root_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override_root = tmp_path / "runtime-override" / ".meridian"
    override_root.parent.mkdir(parents=True, exist_ok=True)
    user_state_root = tmp_path / "user-state"
    user_state_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_state_root.as_posix())
    (user_state_root / "config.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "ctx/work"',
                'archive = "ctx/archive/work"',
                "",
                "[context.kb]",
                'path = "ctx/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    paths = RuntimePaths.from_root_dir(override_root)

    assert paths.work_dir == override_root / "work"
    assert paths.work_archive_dir == override_root / "archive" / "work"
    assert paths.kb_dir == override_root / "kb"


def test_state_root_paths_repo_meridian_stays_runtime_root_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    runtime_root = project_root / ".meridian"
    user_state_root = tmp_path / "user-state"
    project_root.mkdir()
    user_state_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_state_root.as_posix())
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    (project_root / ".git").write_text("gitdir: .git/worktrees/repo\n", encoding="utf-8")
    (project_root / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "ctx/work"',
                'archive = "ctx/archive/work"',
                "",
                "[context.kb]",
                'path = "ctx/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    paths = RuntimePaths.from_root_dir(runtime_root)

    assert paths.work_dir == runtime_root / "work"
    assert paths.work_archive_dir == runtime_root / "archive" / "work"
    assert paths.kb_dir == runtime_root / "kb"


def test_resolve_work_scratch_dir_for_project_uses_context_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    user_state_root = tmp_path / "user-state"
    user_state_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_state_root.as_posix())
    (project_root / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "ctx/work"',
                'archive = "ctx/archive/work"',
                "",
                "[context.kb]",
                'path = "ctx/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    work_dir = resolve_work_scratch_dir_for_project(project_root, "w123")

    assert work_dir == project_root / "ctx/work" / "w123"


def test_resolve_project_paths_from_context_uses_custom_paths(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config = ContextConfig.model_validate(
        {
            "work": {
                "path": "contexts/work",
                "archive": "contexts/archive/work",
            },
            "kb": {"path": "contexts/kb"},
        }
    )

    paths = resolve_project_paths_from_context(project_root, context_config=config)

    assert paths.root_dir == project_root / ".meridian"
    assert paths.id_file == project_root / ".meridian" / "id"
    assert paths.work_dir == project_root / "contexts/work"
    assert paths.work_archive_dir == project_root / "contexts/archive/work"
    assert paths.kb_dir == project_root / "contexts/kb"


def test_resolve_project_paths_from_context_falls_back_when_project_placeholder_uninitialized(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config = ContextConfig.model_validate(
        {
            "work": {
                "path": "contexts/{project}/work",
                "archive": "contexts/{project}/archive/work",
            },
            "kb": {"path": "contexts/{project}/kb"},
        }
    )

    paths = resolve_project_paths_from_context(project_root, context_config=config)

    assert paths.root_dir == project_root / ".meridian"
    assert paths.work_dir == project_root / ".meridian" / "work"
    assert paths.work_archive_dir == project_root / ".meridian" / "archive" / "work"
    assert paths.kb_dir == project_root / ".meridian" / "kb"
    assert not (project_root / ".meridian" / "id").exists()


def test_resolve_project_paths_for_write_initializes_project_placeholder_paths(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "contexts/{project}/work"',
                'archive = "contexts/{project}/archive/work"',
                "",
                "[context.kb]",
                'path = "contexts/{project}/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    paths = resolve_project_paths_for_write(project_root)
    project_uuid = (project_root / ".meridian" / "id").read_text(encoding="utf-8").strip()

    assert project_uuid
    assert paths.work_dir == project_root / f"contexts/{project_uuid}/work"
    assert paths.work_archive_dir == project_root / f"contexts/{project_uuid}/archive/work"
    assert paths.kb_dir == project_root / f"contexts/{project_uuid}/kb"


def test_resolve_project_paths_merges_context_config_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    home_root = tmp_path / "home"
    project_root.mkdir()
    home_root.mkdir()

    user_config_dir = home_root / ".meridian"
    user_config_dir.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_config_dir.as_posix())
    (user_config_dir / "config.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "user/work"',
                "",
                "[context.kb]",
                'path = "user/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (project_root / "meridian.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'archive = "project/archive/work"',
                "",
                "[context.kb]",
                'path = "project/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "local/work"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    paths = resolve_project_paths(project_root)

    assert paths.work_dir == project_root / "local/work"
    assert paths.work_archive_dir == project_root / "project/archive/work"
    assert paths.kb_dir == project_root / "project/kb"


def test_load_context_config_uses_meridian_config_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    user_state_root = tmp_path / "user-state"
    user_state_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_state_root.as_posix())
    (user_state_root / "config.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "home/work"',
                "",
                "[context.kb]",
                'path = "home/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    env_user_config = tmp_path / "env-user-config.toml"
    env_user_config.write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "env/work"',
                'archive = "env/archive/work"',
                "",
                "[context.kb]",
                'path = "env/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_CONFIG", env_user_config.as_posix())

    context_config = load_context_config(project_root)
    resolved_paths = resolve_project_paths(project_root)

    assert context_config is not None
    assert context_config.work.path == "env/work"
    assert context_config.work.archive == "env/archive/work"
    assert context_config.kb.path == "env/kb"
    assert resolved_paths.work_dir == project_root / "env/work"
    assert resolved_paths.work_archive_dir == project_root / "env/archive/work"
    assert resolved_paths.kb_dir == project_root / "env/kb"
