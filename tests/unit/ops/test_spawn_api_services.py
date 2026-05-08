from __future__ import annotations

from pathlib import Path

from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.ops.spawn import api as spawn_api


def test_resolve_spawn_operation_services_prefers_prepared_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    sentinel_service = object()

    monkeypatch.setattr(
        spawn_api,
        "build_spawn_application_service",
        lambda _prepared: sentinel_service,
    )

    services = spawn_api.resolve_spawn_operation_services(
        project_root=None,
        prepared=prepared,
    )

    assert services.project_root == project_root
    assert services.runtime_root == prepared.runtime_root
    assert services.spawn_service is sentinel_service


def test_resolve_spawn_operation_services_builds_from_roots_without_prepared(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    captured: dict[str, Path] = {}
    sentinel_service = object()

    monkeypatch.setattr(
        spawn_api,
        "resolve_runtime_root_and_config",
        lambda _project_root: (project_root, object()),
    )
    monkeypatch.setattr(spawn_api, "resolve_runtime_root", lambda _project_root: runtime_root)

    def _build_from_roots(project: Path, runtime: Path) -> object:
        captured["project_root"] = project
        captured["runtime_root"] = runtime
        return sentinel_service

    monkeypatch.setattr(spawn_api, "build_spawn_application_service_from_roots", _build_from_roots)

    services = spawn_api.resolve_spawn_operation_services(project_root="ignored", prepared=None)

    assert services.project_root == project_root
    assert services.runtime_root == runtime_root
    assert services.spawn_service is sentinel_service
    assert captured == {
        "project_root": project_root,
        "runtime_root": runtime_root,
    }
