import logging
from pathlib import Path

import pytest

from meridian.lib.config.project_config_state import resolve_project_config_state
from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.config.project_root import (
    resolve_project_root,
    resolve_project_root_resolution,
)
from meridian.lib.config.settings import load_config
from meridian.lib.ops.config import ConfigShowInput, config_show_sync
from meridian.lib.ops.config_surface import build_config_surface
from meridian.lib.ops.runtime import resolve_project_authority, resolve_runtime_authority_for_read


def test_resolve_project_root_prefers_mars_skills_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "feature"
    (project_root / ".mars" / "skills").mkdir(parents=True)
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert resolve_project_root() == project_root.resolve()


def test_resolve_project_root_stops_at_git_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "feature"
    project_root.mkdir()
    nested.mkdir(parents=True)
    (project_root / ".git").mkdir()
    monkeypatch.chdir(nested)

    assert resolve_project_root() == project_root.resolve()


def test_resolve_project_root_prefers_meridian_toml_in_plain_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "plain-project"
    nested = project_root / "src" / "feature"
    project_root.mkdir()
    nested.mkdir(parents=True)
    (project_root / "meridian.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(nested)

    resolution = resolve_project_root_resolution()

    assert resolution.project_root == project_root.resolve()
    assert resolution.execution_cwd == nested.resolve()
    assert resolution.source == "meridian-toml"


def test_resolve_project_root_prefers_project_state_in_plain_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "plain-project"
    nested = project_root / "pkg" / "module"
    project_state_dir = project_root / ".meridian"
    project_state_dir.mkdir(parents=True)
    (project_state_dir / "id").write_text("project-uuid", encoding="utf-8")
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    resolution = resolve_project_root_resolution()

    assert resolution.project_root == project_root.resolve()
    assert resolution.source == "project-state"


def test_resolve_project_root_does_not_treat_user_home_state_dir_as_project_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_root = tmp_path / "home"
    nested = home_root / "notes" / "daily"
    (home_root / ".meridian").mkdir(parents=True)
    nested.mkdir(parents=True)
    monkeypatch.setenv("HOME", home_root.as_posix())
    monkeypatch.delenv("MERIDIAN_HOME", raising=False)
    monkeypatch.chdir(nested)

    resolution = resolve_project_root_resolution()

    assert resolution.project_root != home_root.resolve()
    assert resolution.source != "project-state"


def test_load_config_reads_meridian_toml_at_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config_path = project_root / "meridian.toml"
    config_path.write_text("[defaults]\nharness = \"claude\"\n", encoding="utf-8")

    assert load_config(project_root).default_harness == "claude"


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
                "model = \"gpt-5.4\"",
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


def test_load_config_ignores_legacy_state_path_when_root_config_missing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    legacy_path = project_root / ".meridian" / "config.toml"
    legacy_path.parent.mkdir()
    legacy_path.write_text("[defaults]\nharness = \"claude\"\n", encoding="utf-8")

    assert load_config(project_root).default_harness == "codex"


def test_config_show_ignores_inaccessible_implicit_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_root = tmp_path / "repo"
    config_path = tmp_path / "user-home" / "config.toml"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", config_path.parent.as_posix())
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)

    original_is_file = Path.is_file

    def _raise_on_target(self: Path) -> bool:
        if self == config_path:
            raise PermissionError("sandbox denied")
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_file", _raise_on_target)

    with caplog.at_level(logging.WARNING, logger="meridian.lib.config.project_root"):
        result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))

    default_harness = next(item for item in result.values if item.key == "defaults.harness")
    assert default_harness.value == "codex"
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


def test_project_authority_keeps_nested_plain_dir_root_and_paths(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "plain-project"
    nested = project_root / "tools" / "scripts"
    project_root.mkdir()
    nested.mkdir(parents=True)
    (project_root / "meridian.local.toml").write_text("", encoding="utf-8")

    authority = resolve_project_authority(execution_cwd=nested)

    assert authority.project_root == project_root.resolve()
    assert authority.execution_cwd == nested.resolve()
    assert authority.project_root_source == "meridian-local-toml"
    assert authority.project_config_paths.project_root == project_root.resolve()
    assert authority.project_config_paths.execution_cwd == nested.resolve()


def test_project_authority_freezes_workspace_local_path_at_resolution_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "plain-project"
    project_root.mkdir()
    authority = resolve_project_authority(project_root)

    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", (tmp_path / "runtime-b" / ".meridian").as_posix())

    assert authority.project_config_paths.workspace_local_toml == (
        project_root.resolve() / "workspace.local.toml"
    )


def test_build_config_surface_uses_authority_project_config_paths_after_env_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    authority = resolve_project_authority(project_root)
    frozen_workspace_path = tmp_path / "frozen-workspace.local.toml"
    frozen_workspace_root = tmp_path / "frozen-root"
    frozen_workspace_root.mkdir()
    frozen_workspace_path.write_text(
        "[[context-roots]]\n"
        'path = "./frozen-root"\n',
        encoding="utf-8",
    )
    live_workspace_path = project_root / "workspace.local.toml"
    live_workspace_root = project_root / "live-root"
    live_workspace_root.mkdir()
    live_workspace_path.write_text(
        "[[context-roots]]\n"
        'path = "./live-root"\n',
        encoding="utf-8",
    )
    frozen_paths = ProjectConfigPaths(
        project_root=authority.project_root,
        execution_cwd=authority.execution_cwd,
        meridian_toml=authority.project_config_paths.meridian_toml,
        workspace_local_toml=frozen_workspace_path,
        meridian_local_toml=authority.project_config_paths.meridian_local_toml,
    )
    frozen_authority = authority.model_copy(update={"project_config_paths": frozen_paths})

    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", (tmp_path / "runtime-after").as_posix())

    surface = build_config_surface(frozen_authority)

    assert surface.workspace.sources == (frozen_workspace_path.resolve().as_posix(),)
    assert surface.workspace.roots_detail[0].resolved_path == (
        frozen_workspace_root.resolve().as_posix()
    )


def test_runtime_authority_for_read_falls_back_to_project_state_in_plain_directory(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "plain-project"
    nested = project_root / "docs"
    runtime_root = project_root / ".meridian"
    nested.mkdir(parents=True)
    runtime_root.mkdir()
    (runtime_root / "id").write_text("project-uuid", encoding="utf-8")
    (runtime_root / "spawns.jsonl").write_text("", encoding="utf-8")

    authority = resolve_runtime_authority_for_read(execution_cwd=nested)

    assert authority.project_root == project_root.resolve()
    assert authority.runtime_root == runtime_root.resolve()
    assert authority.runtime_root_source in {"project-state", "project-state-fallback"}


def test_config_show_preserves_discovered_project_root_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "plain-project"
    nested = project_root / "tools" / "scripts"
    project_root.mkdir()
    nested.mkdir(parents=True)
    (project_root / "meridian.local.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(nested)

    result = config_show_sync(ConfigShowInput())

    assert result.project_root == project_root.resolve().as_posix()
    assert result.project_root_source == "meridian-local-toml"
