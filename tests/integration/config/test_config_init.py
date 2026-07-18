# qa-validated: test-suite-redesign
"""Config initialization and runtime bootstrap tests."""

from pathlib import Path

import pytest

from meridian.lib.ops.config import (
    ConfigInitInput,
    config_init_sync,
    ensure_runtime_state_bootstrap_sync,
)
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def test_config_init_creates_meridian_toml_and_is_idempotent(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    config_path = project_root / "meridian.toml"

    first = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))
    config_path.write_text("[defaults]\nmax_depth = 7\n", encoding="utf-8")
    second = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))

    assert first.created is True
    assert second.created is False
    assert first.path == config_path.as_posix()
    assert second.path == config_path.as_posix()
    assert config_path.is_file()
    content = config_path.read_text(encoding="utf-8")
    assert content.startswith("[defaults]\nmax_depth = 7\n")
    assert "[project]" in content
    assert not (project_root / "mars.toml").exists()
    assert not (project_root / ".mars").exists()


def test_config_init_scaffold_includes_dynamic_section_examples(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    result = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))
    content = Path(result.path).read_text(encoding="utf-8")

    assert result.created is True
    assert "[defaults]" in content
    assert "[state]" in content
    assert "# [[hooks]]" in content
    assert "# [context.work]" in content
    assert "# [workspace.docs]" in content


def test_config_init_uses_env_project_root_when_path_not_provided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_project_root = _repo(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", env_project_root.as_posix())
    monkeypatch.chdir(cwd)

    result = config_init_sync(ConfigInitInput())

    assert result.path == (env_project_root / "meridian.toml").as_posix()
    assert (env_project_root / "meridian.toml").is_file()
    assert not (cwd / "meridian.toml").exists()
    assert not (env_project_root / "mars.toml").exists()


def test_runtime_bootstrap_creates_identity_without_repo_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    user_state_root = tmp_path / "user-state"
    monkeypatch.setenv("MERIDIAN_HOME", user_state_root.as_posix())

    ensure_runtime_state_bootstrap_sync(project_root)
    runtime_root = resolve_project_runtime_root_for_write(project_root)

    import tomllib

    assert not (project_root / ".meridian").exists()
    project_uuid = tomllib.loads(
        (project_root / "meridian.toml").read_text(encoding="utf-8")
    )["project"]["id"]
    assert runtime_root == user_state_root / "projects" / project_uuid
    assert runtime_root.is_dir()
    assert (runtime_root / "spawns").is_dir()
    assert (project_root / "meridian.toml").is_file()
    assert not (project_root / ".mars").exists()
    assert not (project_root / "mars.toml").exists()


def test_runtime_bootstrap_skips_context_dirs_for_git_backed_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed context directories are created by git-autosync hooks, not bootstrap.

    Bootstrap should skip creating these directories because:
    1. The clone doesn't exist yet (lazy clone approach)
    2. Creating dirs before clone would leave non-git directories at clone paths
    """
    project_root = _repo(tmp_path)
    remote = "https://example.com/acme/context.git"
    clone_path = tmp_path / "clones" / "context"
    (project_root / "meridian.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'source = "git"',
                f'remote = "{remote}"',
                'path = ".meridian/work"',
                'archive = ".meridian/archive/work"',
                "",
                "[context.kb]",
                'source = "git"',
                f'remote = "{remote}"',
                'path = ".meridian/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    def _resolve_clone_path(repo_url: str) -> Path:
        assert repo_url == remote
        return clone_path

    monkeypatch.setattr("meridian.lib.context.resolver.resolve_clone_path", _resolve_clone_path)

    ensure_runtime_state_bootstrap_sync(project_root)

    assert not (project_root / ".meridian").exists()

    # Git-backed context directories are NOT created (no clone exists yet)
    assert not clone_path.exists()
    assert not (clone_path / ".meridian" / "work").exists()
    assert not (clone_path / ".meridian" / "archive" / "work").exists()
    assert not (clone_path / ".meridian" / "kb").exists()


@pytest.mark.parametrize("remote_line", ["", 'remote = ""\n'])
def test_runtime_bootstrap_git_source_without_remote_falls_back_to_local_dirs(
    tmp_path: Path,
    remote_line: str,
) -> None:
    project_root = _repo(tmp_path)
    # Explicit project-local paths so assertions work regardless of default changes.
    # The key behavior under test: source=git with no remote should not attempt a
    # clone, and should create dirs at the configured paths instead.
    (project_root / "meridian.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'source = "git"',
                'path = ".meridian/work"',
                'archive = ".meridian/archive/work"',
                remote_line.rstrip("\n"),
                "",
                "[context.kb]",
                'source = "git"',
                'path = ".meridian/kb"',
                remote_line.rstrip("\n"),
                "",
            ]
        ),
        encoding="utf-8",
    )

    ensure_runtime_state_bootstrap_sync(project_root)

    assert (project_root / ".meridian").is_dir()
    assert (project_root / ".meridian" / "work").is_dir()
    assert (project_root / ".meridian" / "archive" / "work").is_dir()
    assert (project_root / ".meridian" / "kb").is_dir()
