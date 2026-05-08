"""Shared launch-failure finalization policy.

All launch_failure finalization in the execute surface routes through this module.
The fixed terminal tuple is: status="failed", exit_code=1, origin="launch_failure".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
from meridian.lib.core.spawn_service import CompleteSpawnOutcome
from meridian.lib.core.types import SpawnId


async def finalize_launch_failure(
    runtime_root: Path,
    project_root: Path,
    spawn_id: SpawnId,
    error: str,
) -> CompleteSpawnOutcome:
    """Finalize a spawn as launch_failure. Owns the fixed tuple."""
    service = build_spawn_application_service_from_roots(project_root, runtime_root)
    return await service.complete_spawn(
        spawn_id,
        "failed",
        1,
        origin="launch_failure",
        error=error,
    )


def finalize_launch_failure_sync(
    runtime_root: Path,
    project_root: Path,
    spawn_id: SpawnId,
    error: str,
) -> CompleteSpawnOutcome:
    """Synchronous variant for non-async call sites."""
    return asyncio.run(finalize_launch_failure(runtime_root, project_root, spawn_id, error))
