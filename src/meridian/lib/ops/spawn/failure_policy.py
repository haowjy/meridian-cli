"""Shared launch-failure finalization policy.

All launch_failure finalization in the execute surface routes through this module.
The fixed terminal tuple is: status="failed", exit_code=1, origin="launch_failure".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
from meridian.lib.core.clock import RealClock
from meridian.lib.core.native_identity import NativeEntryMismatch, NativeSessionUnavailable
from meridian.lib.core.spawn_service import CompleteSpawnOutcome
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.artifact_io import append_runner_lifecycle_event
from meridian.lib.launch.constants import RUNNER_LIFECYCLE_FILENAME
from meridian.lib.state.paths import resolve_spawn_log_dir


async def finalize_launch_failure(
    runtime_root: Path,
    project_root: Path,
    spawn_id: SpawnId,
    error: str | Exception,
) -> CompleteSpawnOutcome:
    """Finalize a spawn as launch_failure. Owns the fixed tuple."""
    if isinstance(error, NativeEntryMismatch):
        append_runner_lifecycle_event(
            runtime_root, spawn_id,
            resolve_spawn_log_dir(project_root, spawn_id, runtime_root=runtime_root)
            / RUNNER_LIFECYCLE_FILENAME,
            clock=RealClock(), event="entry_mismatch", phase="launch_failure",
            expected=error.expected, observed=error.observed,
        )
    service = build_spawn_application_service_from_roots(project_root, runtime_root)
    return await service.complete_spawn(
        spawn_id,
        "failed",
        1,
        origin="launch_failure",
        error=(
            error.failure_code
            if isinstance(error, (NativeSessionUnavailable, NativeEntryMismatch)) else str(error)
        ),
    )


def finalize_launch_failure_sync(
    runtime_root: Path,
    project_root: Path,
    spawn_id: SpawnId,
    error: str | Exception,
) -> CompleteSpawnOutcome:
    """Synchronous variant for non-async call sites."""
    return asyncio.run(finalize_launch_failure(runtime_root, project_root, spawn_id, error))
