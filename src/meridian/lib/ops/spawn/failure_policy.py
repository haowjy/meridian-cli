"""Shared launch-failure finalization policy.

All launch_failure finalization in the execute surface routes through this module.
The fixed terminal tuple is: status="failed", exit_code=1, origin="launch_failure".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
from meridian.lib.core.native_identity import NativeIdentityError
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.spawn_service import CompleteSpawnOutcome
from meridian.lib.core.types import RunId
from meridian.lib.launch.artifact_io import LifecycleLog, record_identity_failure
from meridian.lib.state.session_store import ChatBusy


async def finalize_launch_failure(
    runtime_root: Path,
    project_root: Path,
    spawn_id: RunId,
    error: str | Exception,
) -> CompleteSpawnOutcome:
    """Finalize a spawn as launch_failure. Owns the fixed tuple."""
    if isinstance(error, NativeIdentityError):
        record_identity_failure(
            error,
            phase="launch_failure",
            lifecycle=LifecycleLog.for_spawn(runtime_root, project_root, spawn_id),
        )
    service = build_spawn_application_service_from_roots(project_root, runtime_root)
    return await service.complete_spawn(
        parse_ref(spawn_id, row_key=True),
        "failed",
        1,
        origin="launch_failure",
        error=(error.failure_code if isinstance(error, NativeIdentityError)
               else "chat_busy" if isinstance(error, ChatBusy) else str(error)),
    )


def finalize_launch_failure_sync(
    runtime_root: Path,
    project_root: Path,
    spawn_id: RunId,
    error: str | Exception,
) -> CompleteSpawnOutcome:
    """Synchronous variant for non-async call sites."""
    return asyncio.run(finalize_launch_failure(runtime_root, project_root, spawn_id, error))
