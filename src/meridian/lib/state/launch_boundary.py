"""Durable launch-boundary observability for background spawn startup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.types import HarnessSessionId, SpawnId
from meridian.lib.state.event_store import append_event, utc_now_iso
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

LAUNCH_BOUNDARY_FILENAME = "launch-boundary.jsonl"

EVENT_PARENT_LAUNCH_ATTEMPT = "parent_launch_attempt"
EVENT_PARENT_LAUNCH_SPAWNED = "parent_launch_spawned"
EVENT_PARENT_LAUNCH_FAILED = "parent_launch_failed"
EVENT_WORKER_BOOT = "worker_boot"
EVENT_WORKER_REQUEST_LOADED = "worker_request_loaded"
EVENT_WORKER_TAKEOVER_STARTED = "worker_takeover_started"
EVENT_WORKER_FAILURE = "worker_failure"
EVENT_HARNESS_SESSION_OBSERVED = "harness_session_observed"

class LaunchBoundaryEvent(BaseModel):
    """One append-only launch boundary observation."""

    model_config = ConfigDict(frozen=True)

    v: int = 1
    ts: str
    event: str
    stage: str | None = None
    parent_pid: int | None = None
    launcher_pid: int | None = None
    worker_pid: int | None = None
    harness_session_id: HarnessSessionId | None = None
    command: tuple[str, ...] | None = None
    cwd: str | None = None
    error: str | None = None
    exception_type: str | None = None
    details: dict[str, Any] | None = None

def launch_boundary_path(runtime_root: Path, spawn_id: SpawnId) -> Path:
    return runtime_root / "spawns" / str(spawn_id) / LAUNCH_BOUNDARY_FILENAME


def launch_boundary_lock_path(runtime_root: Path, spawn_id: SpawnId) -> Path:
    return runtime_root / "locks" / "launch-boundary" / f"{spawn_id!s}.lock"


def record_launch_boundary_event(
    runtime_root: Path,
    spawn_id: SpawnId,
    *,
    event: str,
    stage: str | None = None,
    parent_pid: int | None = None,
    launcher_pid: int | None = None,
    worker_pid: int | None = None,
    harness_session_id: HarnessSessionId | None = None,
    command: tuple[str, ...] | None = None,
    cwd: str | None = None,
    error: str | None = None,
    exception_type: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    data_path = launch_boundary_path(runtime_root, spawn_id)

    def append_observation() -> None:
        append_event(
            data_path,
            launch_boundary_lock_path(runtime_root, spawn_id),
            LaunchBoundaryEvent(
                ts=utc_now_iso(),
                event=event,
                stage=stage,
                parent_pid=parent_pid,
                launcher_pid=launcher_pid,
                worker_pid=worker_pid,
                harness_session_id=harness_session_id,
                command=command,
                cwd=cwd,
                error=error,
                exception_type=exception_type,
                details=details,
            ),
            exclude_none=True,
        )

    mutate_published_spawn_artifact(runtime_root, spawn_id, append_observation)


__all__ = [
    "EVENT_HARNESS_SESSION_OBSERVED",
    "EVENT_PARENT_LAUNCH_ATTEMPT",
    "EVENT_PARENT_LAUNCH_FAILED",
    "EVENT_PARENT_LAUNCH_SPAWNED",
    "EVENT_WORKER_BOOT",
    "EVENT_WORKER_FAILURE",
    "EVENT_WORKER_REQUEST_LOADED",
    "EVENT_WORKER_TAKEOVER_STARTED",
    "LAUNCH_BOUNDARY_FILENAME",
    "LaunchBoundaryEvent",
    "launch_boundary_lock_path",
    "launch_boundary_path",
    "record_launch_boundary_event",
]
