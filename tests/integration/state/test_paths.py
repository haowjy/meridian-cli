from pathlib import Path

import pytest

from meridian.lib.config.context_config import ContextConfig
from meridian.lib.state.paths import (
    RuntimePaths,
    load_context_config,
    resolve_project_paths,
    resolve_project_paths_for_write,
    resolve_project_paths_from_context,
)


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
    import tomllib

    project_uuid = tomllib.loads(
        (project_root / "meridian.toml").read_text(encoding="utf-8")
    )["project"]["id"]

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


def test_resolve_project_paths_does_not_fall_back_to_repo_local_state(
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

    assert paths.work_dir is None
    assert paths.work_archive_dir is None
    assert paths.kb_dir is None
    assert not (project_root / ".meridian").exists()
    assert not (project_root / "meridian.toml").exists()
