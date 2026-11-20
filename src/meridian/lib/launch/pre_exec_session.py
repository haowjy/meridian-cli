from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from meridian.lib.core.native_identity import PrecreatedSession
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import PreExecSessionRequest, SubprocessHarness
from meridian.lib.launch.context import PreparedLaunchSurface
from meridian.lib.launch.env import inherit_child_env
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import LaunchPaths


class LaunchCancelledError(RuntimeError):
    """A cancel was requested before the session was created."""


def create_session_before_exec(
    *,
    adapter: SubprocessHarness,
    operation: Literal["create", "fork"],
    source_session_id: str | None,
    native_store: str | None,
    surface: PreparedLaunchSurface | None,
    cwd: Path,
    runtime_root: Path,
    launch: LaunchPaths,
) -> PrecreatedSession | None:
    """Run the adapter's create-before-exec port and record the session on the spawn row.

    The caller writes the spawn row first and calls this on a fresh launch (``create``) or a
    fork, never on a resume. Returns the session and how it came to exist, or ``None`` when
    the adapter creates nothing. The ID is recorded the moment the session exists, so a
    launch killed before exec leaves a row that names the session instead of an unrecorded
    one. A failure propagates and fails the launch; nothing is retried or cleaned up here.
    """
    if launch.cancel.exists():
        raise LaunchCancelledError(f"{launch.spawn_id} was cancelled before its session started")

    created = adapter.create_session_before_exec(
        PreExecSessionRequest(
            operation=operation,
            source_session_id=source_session_id,
            native_store=native_store,
            surface=surface,
            cwd=cwd,
            env=inherit_child_env(os.environ, None),
        )
    )
    if created is None:
        return None
    session_id = created.strip()
    if not session_id:
        raise RuntimeError("Harness adapter.create_session_before_exec() returned an empty ID.")

    spawn_store.update_spawn(runtime_root, SpawnId(launch.spawn_id), harness_session_id=session_id)
    return PrecreatedSession(session_id, operation)
