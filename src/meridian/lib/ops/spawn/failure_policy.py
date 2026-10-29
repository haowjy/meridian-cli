"""Shared launch-failure finalization policy.

All launch_failure finalization in the execute surface routes through this module.
The fixed terminal tuple is: status="failed", exit_code=1, origin="launch_failure".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
from meridian.lib.core.native_identity import NativeIdentityError
from meridian.lib.core.spawn_service import CompleteSpawnOutcome
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.artifact_io import LifecycleLog, record_identity_failure
from meridian.lib.state.paths import LaunchPaths


async def finalize_launch_failure(
    runtime_root: Path,
    project_root: Path,
    spawn_id: SpawnId,
    error: str | Exception,
    *,
    launch: LaunchPaths | None,
) -> CompleteSpawnOutcome:
    """Finalize a spawn as launch_failure. Owns the fixed tuple.

    ``launch`` is None only when a background worker failed before it could read its row;
    an identity failure then has no lifecycle log to land in.
    """
    if isinstance(error, NativeIdentityError) and launch is not None:
        record_identity_failure(
            error,
            phase="launch_failure",
            lifecycle=LifecycleLog.for_launch(launch),
        )
    service = build_spawn_application_service_from_roots(project_root, runtime_root)
    return await service.complete_spawn(
        spawn_id,
        "failed",
        1,
        origin="launch_failure",
        error=(error.failure_code if isinstance(error, NativeIdentityError) else str(error)),
    )


def finalize_launch_failure_sync(
    runtime_root: Path,
    project_root: Path,
    spawn_id: SpawnId,
    error: str | Exception,
    *,
    launch: LaunchPaths | None,
) -> CompleteSpawnOutcome:
    """Synchronous variant for non-async call sites."""
    return asyncio.run(
        finalize_launch_failure(runtime_root, project_root, spawn_id, error, launch=launch)
    )
