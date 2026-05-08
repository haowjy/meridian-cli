"""Unit tests for first-party extension command handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meridian.lib.bootstrap.services import build_extension_entrypoint, prepare_for_runtime_write
from meridian.lib.extensions.commands.sessions import (
    archive_spawn_handler,
    get_spawn_stats_handler,
)
from meridian.lib.extensions.commands.workbench import ping_handler
from meridian.lib.extensions.context import (
    ExtensionCommandServices,
    ExtensionInvocationContext,
    ExtensionInvocationContextBuilder,
    build_extension_command_services,
)
from meridian.lib.extensions.registry import build_first_party_registry
from meridian.lib.extensions.types import (
    ExtensionErrorResult,
    ExtensionJSONResult,
    ExtensionSurface,
)
from meridian.lib.ops.spawn.models import SpawnStatsInput, SpawnStatsOutput


def _build_context() -> ExtensionInvocationContext:
    return (
        ExtensionInvocationContextBuilder(ExtensionSurface.HTTP)
        .with_project_uuid("project-uuid")
        .build()
    )


def _build_application(tmp_path: Path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    return build_extension_entrypoint(prepared)


def test_first_party_registry_contains_wrapped_operations() -> None:
    registry = build_first_party_registry()
    by_fqid = {spec.fqid: spec for spec in registry.list_all()}
    expected_surfaces = {
        "meridian.sessions.archiveSpawn": {"cli", "http", "mcp"},
        "meridian.sessions.getSpawnStats": {"cli", "http", "mcp"},
        "meridian.workbench.ping": {"cli", "http", "mcp"},
        "meridian.config.show": {"cli", "http"},
        "meridian.spawn.create": {"http", "mcp"},
        "meridian.work.list": {"cli", "http"},
    }

    for fqid, surfaces in expected_surfaces.items():
        assert fqid in by_fqid
        assert {surface.value for surface in by_fqid[fqid].surfaces} == surfaces


@pytest.mark.asyncio
async def test_ping_handler_returns_ok_true() -> None:
    result = await ping_handler({}, _build_context(), ExtensionCommandServices())

    assert isinstance(result, ExtensionJSONResult)
    assert result.payload == {"ok": True}


@pytest.mark.asyncio
async def test_archive_spawn_handler_routes_through_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Handler routes archive through SpawnApplicationService.archive (SEAM-5)."""
    from meridian.lib.core import spawn_service as service_mod

    captured: list[str] = []

    async def _fake_archive(self: object, spawn_id: str) -> bool:
        _ = self
        captured.append(spawn_id)
        return True  # was_new=True

    monkeypatch.setattr(service_mod.SpawnApplicationService, "archive", _fake_archive)
    application = _build_application(tmp_path)

    result = await archive_spawn_handler(
        {"spawn_id": "p123"},
        _build_context(),
        build_extension_command_services(application=application),
    )

    assert isinstance(result, ExtensionJSONResult)
    assert result.payload == {
        "spawn_id": "p123",
        "archived": True,
        "was_already_archived": False,
    }
    assert captured == ["p123"]


@pytest.mark.asyncio
async def test_archive_spawn_handler_reports_already_archived(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Handler correctly reports when spawn was already archived."""
    from meridian.lib.core import spawn_service as service_mod

    async def _fake_archive(self: object, spawn_id: str) -> bool:
        _ = (self, spawn_id)
        return False  # was_new=False (already archived)

    monkeypatch.setattr(service_mod.SpawnApplicationService, "archive", _fake_archive)
    application = _build_application(tmp_path)

    result = await archive_spawn_handler(
        {"spawn_id": "p456"},
        _build_context(),
        build_extension_command_services(application=application),
    )

    assert isinstance(result, ExtensionJSONResult)
    assert result.payload == {
        "spawn_id": "p456",
        "archived": True,
        "was_already_archived": True,
    }


@pytest.mark.asyncio
async def test_archive_spawn_handler_returns_error_on_invalid_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Handler returns error when archive raises ValueError (non-terminal spawn)."""
    from meridian.lib.core import spawn_service as service_mod

    async def _fake_archive(self: object, spawn_id: str) -> bool:
        _ = (self, spawn_id)
        raise ValueError("Cannot archive non-terminal spawn (status: running)")

    monkeypatch.setattr(service_mod.SpawnApplicationService, "archive", _fake_archive)
    application = _build_application(tmp_path)

    result = await archive_spawn_handler(
        {"spawn_id": "p789"},
        _build_context(),
        build_extension_command_services(application=application),
    )

    assert isinstance(result, ExtensionErrorResult)
    assert result.code == "invalid_state"
    assert "non-terminal" in result.message


@pytest.mark.asyncio
async def test_archive_spawn_handler_uses_extension_application_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    application = build_extension_entrypoint(prepared)
    captured: dict[str, object] = {}

    def _fake_spawn_service(application_arg: object) -> object:
        captured["application"] = application_arg

        class _Service:
            async def archive(self, spawn_id: str) -> bool:
                captured["spawn_id"] = spawn_id
                return True

        return _Service()

    monkeypatch.setattr(
        "meridian.lib.extensions.commands.sessions.build_spawn_application_service_from_entrypoint",
        _fake_spawn_service,
    )

    result = await archive_spawn_handler(
        {"spawn_id": "p321"},
        _build_context(),
        build_extension_command_services(application=application),
    )

    assert isinstance(result, ExtensionJSONResult)
    assert captured["application"] == application
    assert captured["spawn_id"] == "p321"


def test_build_extension_command_services_carries_application_authority(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    application = build_extension_entrypoint(prepared)

    services = build_extension_command_services(application=application)
    context = (
        ExtensionInvocationContextBuilder(ExtensionSurface.CLI)
        .with_authority(prepared.authority)
        .build()
    )

    assert services.authority == prepared.authority
    assert services.application.context.authority == prepared.authority
    assert context.authority == prepared.authority


@pytest.mark.asyncio
async def test_get_spawn_stats_returns_spawn_stats_output_compatible_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import meridian.lib.ops.spawn.api as spawn_api_mod

    captured: dict[str, Any] = {}
    expected = SpawnStatsOutput(
        total_runs=2,
        succeeded=1,
        failed=1,
        cancelled=0,
        running=0,
        finalizing=0,
        total_duration_secs=9.5,
        total_cost_usd=0.1234,
        models={},
        children=(),
    )

    def _fake_spawn_stats_sync(payload: object) -> SpawnStatsOutput:
        captured["payload"] = payload
        return expected

    monkeypatch.setattr(spawn_api_mod, "spawn_stats_sync", _fake_spawn_stats_sync)

    result = await get_spawn_stats_handler(
        {"spawn_id": "p42"},
        _build_context(),
        ExtensionCommandServices(),
    )
    assert isinstance(result, ExtensionErrorResult)
    assert result.code == "service_unavailable"
    assert "project_root not available" in result.message


@pytest.mark.asyncio
async def test_get_spawn_stats_prefers_extension_application_project_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import meridian.lib.ops.spawn.api as spawn_api_mod

    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    application = build_extension_entrypoint(prepared)
    captured: dict[str, Any] = {}

    def _fake_spawn_stats_sync(payload: object) -> SpawnStatsOutput:
        captured["payload"] = payload
        return SpawnStatsOutput(
            total_runs=0,
            succeeded=0,
            failed=0,
            cancelled=0,
            running=0,
            finalizing=0,
            total_duration_secs=0.0,
            total_cost_usd=0.0,
            models={},
            children=(),
        )

    monkeypatch.setattr(spawn_api_mod, "spawn_stats_sync", _fake_spawn_stats_sync)

    result = await get_spawn_stats_handler(
        {"spawn_id": "p84"},
        _build_context(),
        build_extension_command_services(
            application=application,
            meridian_dir=tmp_path / "wrong-parent" / ".meridian",
        ),
    )

    if isinstance(result, ExtensionErrorResult):
        pytest.fail(f"unexpected error result: {result}")

    payload = captured["payload"]
    assert isinstance(payload, SpawnStatsInput)
    assert payload.project_root == project_root.as_posix()


@pytest.mark.asyncio
async def test_get_spawn_stats_falls_back_to_extension_authority_project_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import meridian.lib.ops.spawn.api as spawn_api_mod

    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    captured: dict[str, Any] = {}

    def _fake_spawn_stats_sync(payload: object) -> SpawnStatsOutput:
        captured["payload"] = payload
        return SpawnStatsOutput(
            total_runs=0,
            succeeded=0,
            failed=0,
            cancelled=0,
            running=0,
            finalizing=0,
            total_duration_secs=0.0,
            total_cost_usd=0.0,
            models={},
            children=(),
        )

    monkeypatch.setattr(spawn_api_mod, "spawn_stats_sync", _fake_spawn_stats_sync)

    result = await get_spawn_stats_handler(
        {"spawn_id": "p96"},
        _build_context(),
        build_extension_command_services(authority=prepared.authority),
    )

    if isinstance(result, ExtensionErrorResult):
        pytest.fail(f"unexpected error result: {result}")

    payload = captured["payload"]
    assert isinstance(payload, SpawnStatsInput)
    assert payload.project_root == project_root.as_posix()
