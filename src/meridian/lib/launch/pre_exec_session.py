from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Literal

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import PreExecSessionRequest, SubprocessHarness
from meridian.lib.launch.context import PreparedLaunchSurface
from meridian.lib.launch.env import inherit_child_env
from meridian.lib.platform.signals import signal_to_exit_code
from meridian.lib.state import spawn_store
from meridian.lib.state.native_identity import PrecreatedSession
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn_status import read_cancel_intent


class LaunchCancelledError(RuntimeError):
    """A cancel was requested before the session was created; the launch ends cancelled."""

    def __init__(self, message: str, *, exit_code: int, error: str | None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.error = error


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
    row = spawn_store.get_spawn(runtime_root, SpawnId(launch.spawn_id))
    intent = read_cancel_intent(launch, row) if row is not None else None
    if intent is not None or launch.cancel.exists():
        # An unreadable cancel.json is still a cancel.
        raise LaunchCancelledError(
            f"{launch.spawn_id} was cancelled before its session started",
            exit_code=intent.exit_code if intent else signal_to_exit_code(signal.SIGTERM),
            error=intent.error if intent else None,
        )

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
