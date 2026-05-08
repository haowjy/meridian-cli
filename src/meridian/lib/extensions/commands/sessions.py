"""Session-related first-party extension commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from meridian.lib.extensions.context import (
    ExtensionCommandServices,
    ExtensionInvocationContext,
)
from meridian.lib.extensions.types import (
    ExtensionCommandSpec,
    ExtensionErrorResult,
    ExtensionJSONResult,
    ExtensionResult,
    ExtensionSurface,
)
from meridian.lib.ops.spawn.models import SpawnStatsOutput


class ArchiveSpawnArgs(BaseModel):
    """Arguments for meridian.sessions.archiveSpawn."""

    spawn_id: str = Field(description="ID of spawn to archive")


class ArchiveSpawnResult(BaseModel):
    """Result payload for meridian.sessions.archiveSpawn."""

    spawn_id: str
    archived: bool
    was_already_archived: bool = False


async def archive_spawn_handler(
    args: dict[str, Any],
    context: ExtensionInvocationContext,
    services: ExtensionCommandServices,
) -> ExtensionResult:
    """Archive a spawn ID in runtime state.

    Routes through SpawnApplicationService.archive() for lifecycle policy
    coordination (SEAM-5).
    """
    _ = context
    try:
        services.require_runtime_root()
        spawn_service = services.build_spawn_service()
    except ValueError as exc:
        return ExtensionErrorResult(
            code="service_unavailable",
            message=str(exc),
        )

    spawn_id = args["spawn_id"]

    try:
        was_new = await spawn_service.archive(spawn_id)
        return ExtensionJSONResult(
            payload={
                "spawn_id": spawn_id,
                "archived": True,
                "was_already_archived": not was_new,
            }
        )
    except ValueError as exc:
        return ExtensionErrorResult(
            code="invalid_state",
            message=str(exc),
        )


ARCHIVE_SPAWN_SPEC = ExtensionCommandSpec(
    extension_id="meridian.sessions",
    command_id="archiveSpawn",
    summary="Archive a completed spawn to hide it from default listings",
    args_schema=ArchiveSpawnArgs,
    result_schema=ArchiveSpawnResult,
    handler=archive_spawn_handler,
    surfaces=frozenset(
        {
            ExtensionSurface.CLI,
            ExtensionSurface.MCP,
            ExtensionSurface.HTTP,
        }
    ),
    first_party=True,
    requires_app_server=True,
)


class GetSpawnStatsArgs(BaseModel):
    """Arguments for meridian.sessions.getSpawnStats."""

    spawn_id: str = Field(description="ID of spawn to get stats for")


async def get_spawn_stats_handler(
    args: dict[str, Any],
    context: ExtensionInvocationContext,
    services: ExtensionCommandServices,
) -> ExtensionResult:
    """Wrap spawn_stats operation output for extension command surface."""

    _ = context
    try:
        project_root = services.require_project_root()
    except ValueError as exc:
        return ExtensionErrorResult(
            code="service_unavailable",
            message=str(exc),
        )

    from meridian.lib.ops.spawn.api import spawn_stats_sync
    from meridian.lib.ops.spawn.models import SpawnStatsInput

    payload = SpawnStatsInput(
        spawn_id=args["spawn_id"],
        project_root=project_root.as_posix(),
    )
    result = spawn_stats_sync(payload)
    return ExtensionJSONResult(payload=result.model_dump())


GET_SPAWN_STATS_SPEC = ExtensionCommandSpec(
    extension_id="meridian.sessions",
    command_id="getSpawnStats",
    summary="Get token usage and cost statistics for a spawn",
    args_schema=GetSpawnStatsArgs,
    result_schema=SpawnStatsOutput,
    handler=get_spawn_stats_handler,
    surfaces=frozenset(
        {
            ExtensionSurface.CLI,
            ExtensionSurface.MCP,
            ExtensionSurface.HTTP,
        }
    ),
    first_party=True,
    requires_app_server=True,
)
