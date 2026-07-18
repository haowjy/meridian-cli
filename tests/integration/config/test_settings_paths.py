import logging
from pathlib import Path

import pytest

from meridian.lib.config.project_config_state import resolve_project_config_state
from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.config.settings import load_config
from meridian.lib.ops.config import ConfigShowInput, config_show_sync
from meridian.lib.ops.config_surface import build_config_surface
from meridian.lib.ops.runtime import resolve_project_authority, resolve_runtime_authority_for_read


def test_resolve_project_root_uses_literal_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.chdir(project_root)

    resolution = resolve_project_root_resolution()

    assert resolution.project_root == project_root.resolve()
    assert resolution.execution_cwd == project_root.resolve()
    assert resolution.source == "cwd"


def test_resolve_project_root_nested_cwd_does_not_walk_to_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "feature"
    project_root.mkdir()
    nested.mkdir(parents=True)
    (project_root / "meridian.toml").write_text("", encoding="utf-8")
    (project_root / ".git").mkdir()
    monkeypatch.chdir(nested)

    resolution = resolve_project_root_resolution()

    assert resolution.project_root == nested.resolve()
    assert resolution.execution_cwd == nested.resolve()
    assert resolution.source == "cwd"


def test_resolve_project_root_prefers_meridian_project_dir_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "feature"
    project_root.mkdir()
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())

    resolution = resolve_project_root_resolution()

    assert resolution.project_root == project_root.resolve()
    assert resolution.execution_cwd == nested.resolve()
    assert resolution.source == "env"


def test_resolve_project_root_explicit_wins_over_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    other_root = tmp_path / "other"
    project_root.mkdir()
    other_root.mkdir()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", other_root.as_posix())

    resolution = resolve_project_root_resolution(project_root)

    assert resolution.project_root == project_root.resolve()
    assert resolution.source == "explicit"


def test_resolve_project_root_ignore_env_uses_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src"
    project_root.mkdir()
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())

    resolution = resolve_project_root_resolution(ignore_env=True)

    assert resolution.project_root == nested.resolve()
    assert resolution.source == "cwd"


def test_load_config_reads_primary_agent_at_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config_path = project_root / "meridian.toml"
    config_path.write_text('[primary]\nagent = "reviewer"\n', encoding="utf-8")

    assert load_config(project_root).primary.agent == "reviewer"


def test_load_config_ignores_deleted_defaults_model_and_harness_keys(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "[defaults]\n"
        'model = "legacy-model"\n'
        'harness = "claude"\n'
        "\n"
        "[primary]\n"
        'model = "shadow-model"\n'
        'harness = "opencode"\n',
        encoding="utf-8",
    )

    config = load_config(project_root, resolve_models=False)

    assert not hasattr(config.primary, "model")
    assert not hasattr(config.primary, "harness")


def test_load_config_reads_harness_wait_yield_settings(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config_path = project_root / "meridian.toml"
    config_path.write_text(
        "\n".join(
            [
                "[spawn]",
                "default_wait_yield_seconds = 120",
                "min_wait_yield_seconds = 45",
                "",
                "[harness.claude]",
                "wait_yield_seconds = 270",
                "",
                "[harness.codex]",
                'model = "gpt-5.4"',
                "wait_yield_seconds = 20",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(project_root, resolve_models=False)

    assert config.default_wait_yield_seconds == 120.0
    assert config.min_wait_yield_seconds == 45.0
    assert config.wait_yield_seconds_for_harness("claude") == 270.0
    assert config.wait_yield_seconds_for_harness("codex") == 45.0
    assert config.wait_yield_seconds_for_harness("unknown") == 120.0
    assert config.default_model_for_harness("codex") == "gpt-5.4"


def test_load_config_reads_spawn_deny_headless_harnesses(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config_path = project_root / "meridian.toml"
    config_path.write_text(
        "\n".join(
            [
                "[spawn]",
                'deny_headless_harnesses = ["claude", "pi"]',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(project_root, resolve_models=False)

    assert config.deny_headless_harnesses == ("claude", "pi")


def test_load_config_reads_pi_disable_managed_bash(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config_path = project_root / "meridian.toml"
    config_path.write_text(
        "\n".join(
            [
                "[harness.pi]",
                "disable_managed_bash = true",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(project_root, resolve_models=False)

    assert config.harness.pi.disable_managed_bash is True
    assert config.default_model_for_harness("pi") == ""
    assert config.wait_yield_seconds_for_harness("pi") == config.default_wait_yield_seconds


def test_load_config_rejects_legacy_agents_table_with_migration_error(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config_path = project_root / "meridian.toml"
    config_path.write_text('[agents.reviewer]\nmodel = "gpt55"\n', encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"Legacy section '\[agents\]' is not supported",
    ) as exc_info:
        load_config(project_root, resolve_models=False)

    message = str(exc_info.value)
    assert str(config_path) in message
    assert "define [agents.<name>] under mars.toml or mars.local.toml" in message


def test_config_show_ignores_inaccessible_implicit_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_root = tmp_path / "repo"
    config_path = tmp_path / "user-home" / "config.toml"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", config_path.parent.as_posix())
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)

    original_is_file = Path.is_file

    def _raise_on_target(self: Path) -> bool:
        if self == config_path:
            raise PermissionError("sandbox denied")
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_file", _raise_on_target)

    with caplog.at_level(logging.WARNING, logger="meridian.lib.config.project_root"):
        result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))

    max_depth = next(item for item in result.values if item.key == "defaults.max_depth")
    assert max_depth.value == 3
    assert any(str(config_path) in record.message for record in caplog.records)


def test_resolve_project_config_state_reports_absent_and_write_target(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    state = resolve_project_config_state(project_root)

    assert state.status == "absent"
    assert state.path is None
    assert state.write_path == project_root.resolve() / "meridian.toml"


def test_resolve_project_config_state_ignores_legacy_state_config(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    legacy_path = project_root / ".meridian" / "config.toml"
    legacy_path.parent.mkdir()
    legacy_path.write_text("[defaults]\nmax_depth = 7\n", encoding="utf-8")

    state = resolve_project_config_state(project_root)

    assert state.status == "absent"
    assert state.path is None
    assert state.write_path == project_root.resolve() / "meridian.toml"


def test_resolve_project_config_state_reports_present_when_meridian_toml_exists(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config_path = project_root / "meridian.toml"
    config_path.write_text("[defaults]\nmax_depth = 7\n", encoding="utf-8")

    state = resolve_project_config_state(project_root)

    assert state.status == "present"
    assert state.path == config_path.resolve()
    assert state.write_path == config_path.resolve()


def test_project_authority_keeps_explicit_root_with_nested_execution_cwd(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "plain-project"
    nested = project_root / "tools" / "scripts"
    project_root.mkdir()
    nested.mkdir(parents=True)
    (project_root / "meridian.local.toml").write_text("", encoding="utf-8")

    authority = resolve_project_authority(project_root, execution_cwd=nested)

    assert authority.project_root == project_root.resolve()
    assert authority.execution_cwd == nested.resolve()
    assert authority.project_root_source == "explicit"
    assert authority.project_config_paths.project_root == project_root.resolve()
    assert authority.project_config_paths.execution_cwd == nested.resolve()


def test_project_authority_freezes_meridian_local_path_at_resolution_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "plain-project"
    project_root.mkdir()
    authority = resolve_project_authority(project_root)

    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", (tmp_path / "runtime-b" / ".meridian").as_posix())

    assert authority.project_config_paths.meridian_local_toml == (
        project_root.resolve() / "meridian.local.toml"
    )


def test_build_config_surface_uses_authority_project_config_paths_after_env_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    authority = resolve_project_authority(project_root)
    frozen_local_path = tmp_path / "frozen-meridian.local.toml"
    frozen_workspace_root = tmp_path / "frozen-root"
    frozen_workspace_root.mkdir()
    frozen_local_path.write_text(
        f'[workspace.frozen]\npath = "{frozen_workspace_root.as_posix()}"\n',
        encoding="utf-8",
    )
    live_workspace_path = project_root / "meridian.local.toml"
    live_workspace_root = project_root / "live-root"
    live_workspace_root.mkdir()
    live_workspace_path.write_text(
        '[workspace.live]\npath = "./live-root"\n',
        encoding="utf-8",
    )
    frozen_paths = ProjectConfigPaths(
        project_root=authority.project_root,
        execution_cwd=authority.execution_cwd,
        meridian_toml=authority.project_config_paths.meridian_toml,
        meridian_local_toml=frozen_local_path,
    )
    frozen_authority = authority.model_copy(update={"project_config_paths": frozen_paths})

    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", (tmp_path / "runtime-after").as_posix())

    surface = build_config_surface(frozen_authority)

    assert surface.workspace.sources == (frozen_local_path.resolve().as_posix(),)
    assert surface.workspace.roots_detail[0].resolved_path == (
        frozen_workspace_root.resolve().as_posix()
    )


def test_runtime_authority_for_read_is_unresolved_in_plain_directory(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "plain-project"
    nested = project_root / "docs"
    nested.mkdir(parents=True)

    authority = resolve_runtime_authority_for_read(project_root, execution_cwd=nested)

    assert authority.project_root == project_root.resolve()
    assert authority.runtime_root is None
    assert authority.runtime_root_source == "unresolved"
