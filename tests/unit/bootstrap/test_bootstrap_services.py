from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.bootstrap.services import (
    build_chat_entrypoint,
    build_extension_entrypoint,
    build_spawn_application_service,
    build_spawn_entrypoint,
    prepare_for_project_read,
    prepare_for_project_write,
    prepare_for_runtime_read,
    prepare_for_runtime_write,
)
from meridian.lib.config.project_paths import resolve_project_config_paths
from meridian.lib.state.user_paths import get_project_uuid


@pytest.fixture(autouse=True)
def _isolate_bootstrap_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def test_prepare_for_project_read_does_not_create_state(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    result = prepare_for_project_read(project_root)

    assert result.authority.project_root == project_root
    assert result.authority.project_root_source == "explicit"
    assert result.project_root == project_root
    assert result.layout.project_state_dir == project_root / ".meridian"
    assert not (project_root / ".meridian").exists()


def test_prepare_for_runtime_read_returns_none_without_uuid_or_repo_state(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)

    result = prepare_for_runtime_read(project_root)

    assert result.authority.project_root == project_root
    assert result.authority.runtime_root is None
    assert result.authority.runtime_root_source == "unresolved"
    assert result.runtime_root is None
    assert not (project_root / ".meridian").exists()


def test_prepare_for_runtime_read_falls_back_to_existing_repo_local_state(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    repo_state = project_root / ".meridian"
    repo_state.mkdir()
    (repo_state / "spawns.jsonl").write_text("", encoding="utf-8")

    result = prepare_for_runtime_read(project_root)

    assert result.authority.runtime_root == repo_state
    assert result.authority.runtime_root_source == "project-state"
    assert result.runtime_root == repo_state
    assert not (repo_state / "id").exists()


def test_prepare_for_project_write_runs_project_setup_without_runtime_root(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)

    result = prepare_for_project_write(project_root)

    assert result.authority.project_root == project_root
    assert result.migration_ran is True
    assert (project_root / ".meridian" / ".gitignore").is_file()
    assert (project_root / ".meridian" / "kb").is_dir()
    assert (project_root / ".meridian" / "work").is_dir()
    assert (project_root / ".meridian" / "archive" / "work").is_dir()
    assert not (project_root / ".meridian" / "id").exists()


def test_prepare_for_runtime_write_creates_uuid_and_runtime_dirs(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)

    result = prepare_for_runtime_write(project_root)

    project_uuid = get_project_uuid(project_root / ".meridian")
    assert project_uuid is not None
    assert result.authority.runtime_root == result.runtime_root
    assert result.authority.runtime_root_source == "user-home-project"
    assert result.runtime_root == tmp_path / "user-home" / "projects" / project_uuid
    assert result.runtime_root.is_dir()
    assert (result.runtime_root / "spawns").is_dir()
    assert (result.runtime_root / "sessions").is_dir()
    assert (result.runtime_root / "chats").is_dir()
    assert (result.runtime_root / "telemetry").is_dir()


def test_build_spawn_entrypoint_keeps_runtime_write_context_carrier_only(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    prepared = prepare_for_runtime_write(project_root)

    entrypoint = build_spawn_entrypoint(prepared)

    assert entrypoint.context.project_root == project_root
    assert entrypoint.context.runtime_root == prepared.runtime_root
    assert entrypoint.context.config == prepared.config
    assert entrypoint.services.lifecycle is None


def test_build_chat_entrypoint_keeps_runtime_write_context_carrier_only(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    prepared = prepare_for_runtime_write(project_root)

    entrypoint = build_chat_entrypoint(prepared)

    assert entrypoint.context.project_root == project_root
    assert entrypoint.context.runtime_root == prepared.runtime_root
    assert entrypoint.context.config == prepared.config
    assert entrypoint.services.lifecycle is None


def test_build_chat_entrypoint_accepts_minimal_prepared_runtime_carrier(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    class _PreparedRuntime:
        def __init__(self) -> None:
            self.project_root = project_root
            self.runtime_root = runtime_root
            self.config = None

    entrypoint = build_chat_entrypoint(_PreparedRuntime())  # type: ignore[arg-type]

    assert entrypoint.context.project_root == project_root
    assert entrypoint.context.runtime_root == runtime_root
    assert entrypoint.context.authority is None


def test_build_extension_entrypoint_keeps_runtime_write_context_carrier_only(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    prepared = prepare_for_runtime_write(project_root)

    entrypoint = build_extension_entrypoint(prepared)

    assert entrypoint.context.project_root == project_root
    assert entrypoint.context.runtime_root == prepared.runtime_root
    assert entrypoint.context.config == prepared.config
    assert entrypoint.services.lifecycle is None


def test_prepare_for_runtime_write_uses_authority_paths_for_config_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    frozen_config = tmp_path / "frozen-meridian.toml"
    frozen_config.write_text("[defaults]\nmax_depth = 7\n", encoding="utf-8")
    authority = prepare_for_project_read(project_root).authority.model_copy(
        update={
            "project_config_paths": resolve_project_config_paths(project_root).model_copy(
                update={"meridian_toml": frozen_config}
            )
        }
    )

    monkeypatch.setattr(
        "meridian.lib.bootstrap.services.resolve_runtime_authority_for_write",
        lambda _project_root: authority.model_copy(
            update={"runtime_root": tmp_path / "user-home" / "projects" / "uuid"}
        ),
    )

    result = prepare_for_runtime_write(project_root)

    assert result.config is not None
    assert result.config.max_depth == 7


def test_build_spawn_application_service_uses_shared_entrypoint_seam(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    prepared = prepare_for_runtime_write(project_root)

    service = build_spawn_application_service(prepared)

    assert service.runtime_root == prepared.runtime_root
