"""Background spawn worker: persist/load request, launch detached worker, worker main."""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.bootstrap.services import RuntimeWriteContext, prepare_for_runtime_write
from meridian.lib.config.project_paths import resolve_project_config_paths
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.domain import Spawn, SpawnStatus
from meridian.lib.core.types import ModelId, SpawnId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.state import spawn_store
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.launch_boundary import (
    EVENT_HARNESS_SESSION_OBSERVED,
    EVENT_WORKER_BOOT,
    EVENT_WORKER_FAILURE,
    EVENT_WORKER_REQUEST_LOADED,
    EVENT_WORKER_TAKEOVER_STARTED,
    record_launch_boundary_event,
)
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.telemetry.bootstrap import TelemetryMode, TelemetryPlan, install
from meridian.lib.telemetry.init import setup_telemetry
from meridian.lib.telemetry.observer import register_spawn_telemetry_observer

from ..runtime import OperationRuntime, build_runtime, resolve_runtime_root, runtime_context
from .execute_runner import launch_prepared_spawn
from .failure_policy import finalize_launch_failure, finalize_launch_failure_sync

logger = structlog.get_logger(__name__)
_SETUP_TELEMETRY_COMPAT = setup_telemetry
BACKGROUND_STDOUT_FILENAME = "background-launcher.stdout.log"
BACKGROUND_STDERR_FILENAME = "background-launcher.stderr.log"
_BG_WORKER_REQUEST_FILENAME = "bg-worker-request.json"
_BACKGROUND_RUNTIME_ARTIFACTS = (
    BACKGROUND_STDOUT_FILENAME,
    BACKGROUND_STDERR_FILENAME,
    _BG_WORKER_REQUEST_FILENAME,
)


class BackgroundWorkerLaunchRequest(BaseModel):
    """Background worker request payload persisted to disk."""

    model_config = ConfigDict(frozen=True)

    request: SpawnRequest
    runtime: LaunchRuntime


def _resolve_background_execution_cwd(
    *,
    runtime_request: LaunchRuntime,
    project_root: Path,
    work_id: str | None,
) -> str:
    explicit_task_cwd = runtime_request.resolved_requested_task_cwd
    if explicit_task_cwd:
        return explicit_task_cwd
    _ = work_id
    return project_root.as_posix()


def _cleanup_background_runtime_artifacts(log_dir: Path) -> None:
    """Remove non-durable launcher artifacts after terminal completion."""
    for name in _BACKGROUND_RUNTIME_ARTIFACTS:
        target = log_dir / name
        with suppress(FileNotFoundError):
            target.unlink()


def _record_launch_boundary_observation(
    runtime_root: Path,
    spawn_id: str,
    *,
    event: str,
    stage: str | None = None,
    parent_pid: int | None = None,
    launcher_pid: int | None = None,
    worker_pid: int | None = None,
    harness_session_id: str | None = None,
    command: tuple[str, ...] | None = None,
    cwd: str | None = None,
    error: str | None = None,
    exception: BaseException | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        record_launch_boundary_event(
            runtime_root,
            spawn_id,
            event=event,
            stage=stage,
            parent_pid=parent_pid,
            launcher_pid=launcher_pid,
            worker_pid=worker_pid,
            harness_session_id=harness_session_id,
            command=command,
            cwd=cwd,
            error=error,
            exception_type=type(exception).__name__ if exception is not None else None,
            details=details,
        )
    except Exception:
        logger.warning(
            "Failed to persist launch-boundary observation.",
            spawn_id=spawn_id,
            event=event,
            stage=stage,
            exc_info=True,
        )


def _persist_bg_worker_request(log_dir: Path, payload: BackgroundWorkerLaunchRequest) -> None:
    """Write background worker launch request to the spawn log directory."""

    params_path = log_dir / _BG_WORKER_REQUEST_FILENAME
    atomic_write_text(params_path, payload.model_dump_json(indent=2) + "\n")


def _load_bg_worker_request(log_dir: Path) -> BackgroundWorkerLaunchRequest:
    """Load background worker launch request from the spawn log directory."""

    params_path = log_dir / _BG_WORKER_REQUEST_FILENAME
    return BackgroundWorkerLaunchRequest.model_validate_json(
        params_path.read_text(encoding="utf-8")
    )


def _build_background_worker_command(
    *,
    spawn_id: str,
    project_paths: Any,
) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "meridian.lib.ops.spawn.execute_bg",
        "--spawn-id",
        spawn_id,
        "--project-root",
        project_paths.project_root.as_posix(),
    )


async def _execute_existing_spawn(
    *,
    spawn_id: SpawnId,
    project_paths: Any,
    launch_request: BackgroundWorkerLaunchRequest,
    sink: Any | None = None,
    ctx: RuntimeContext | None = None,
    prepared: RuntimeWriteContext | None = None,
) -> int:
    resolved_context = runtime_context(ctx)
    if prepared is not None:
        if prepared.config is None:
            raise ValueError("Prepared runtime write context is missing config.")
        if prepared.runtime_root is None:
            raise ValueError("Prepared runtime write context is missing runtime root.")
        runtime = OperationRuntime.from_prepared(
            prepared,
            harness_registry=get_default_harness_registry(),
            sink=sink,
        )
        runtime_root = prepared.runtime_root
    else:
        runtime = build_runtime(str(project_paths.project_root), sink=sink)
        runtime_root = resolve_runtime_root(project_paths.project_root)
    spawn_record = spawn_store.get_spawn(runtime_root, spawn_id)
    if spawn_record is None:
        logger.error("Spawn not found for background execution.", spawn_id=str(spawn_id))
        return 1

    _record_launch_boundary_observation(
        runtime_root,
        str(spawn_id),
        event=EVENT_WORKER_BOOT,
        stage="worker_boot",
        launcher_pid=(
            spawn_record.runner_pid
            if spawn_record.runner_pid is not None and spawn_record.runner_pid > 0
            else None
        ),
        worker_pid=os.getpid(),
    )

    request = launch_request.request
    runtime_request = launch_request.runtime
    resolved_model = (request.model or "").strip()
    resolved_harness_id = (request.harness or "").strip()
    resolved_prompt = (request.prompt or "").strip()
    if not resolved_prompt:
        _record_launch_boundary_observation(
            runtime_root,
            str(spawn_id),
            event=EVENT_WORKER_FAILURE,
            stage="validate_request",
            worker_pid=os.getpid(),
            error="Missing prompt",
        )
        await finalize_launch_failure(
            runtime_root,
            project_paths.project_root,
            spawn_id,
            "Missing prompt",
        )
        return 1
    if not resolved_harness_id:
        _record_launch_boundary_observation(
            runtime_root,
            str(spawn_id),
            event=EVENT_WORKER_FAILURE,
            stage="validate_request",
            worker_pid=os.getpid(),
            error="Missing harness",
        )
        await finalize_launch_failure(
            runtime_root,
            project_paths.project_root,
            spawn_id,
            "Missing harness",
        )
        return 1

    _record_launch_boundary_observation(
        runtime_root,
        str(spawn_id),
        event=EVENT_WORKER_REQUEST_LOADED,
        stage="request_loaded",
        worker_pid=os.getpid(),
    )

    spawn_status: SpawnStatus = (
        spawn_record.status if spawn_record.status != "unknown" else "queued"
    )
    spawn = Spawn(
        spawn_id=SpawnId(spawn_record.id),
        prompt=resolved_prompt,
        model=ModelId(resolved_model),
        status=spawn_status,
    )

    resolved_execution_cwd = _resolve_background_execution_cwd(
        runtime_request=runtime_request,
        project_root=project_paths.project_root,
        work_id=spawn_record.work_id,
    )

    _record_launch_boundary_observation(
        runtime_root,
        str(spawn_id),
        event=EVENT_WORKER_TAKEOVER_STARTED,
        stage="launch_prepared_spawn",
        worker_pid=os.getpid(),
    )

    return await launch_prepared_spawn(
        spawn=spawn,
        request=request.model_copy(
            update={
                "model": resolved_model,
                "harness": resolved_harness_id,
                "prompt": resolved_prompt,
                "agent": request.agent if request.agent is not None else spawn_record.agent,
                "skill_paths": spawn_record.skill_paths,
            }
        ),
        runtime_request=runtime_request,
        runtime=runtime,
        runtime_root=runtime_root,
        project_paths=project_paths,
        spawn_record=spawn_record,
        execution_cwd=resolved_execution_cwd,
        work_id=spawn_record.work_id,
        harness_session_id_observer=lambda session_id: _record_launch_boundary_observation(
            runtime_root,
            str(spawn_id),
            event=EVENT_HARNESS_SESSION_OBSERVED,
            stage="session_observed",
            worker_pid=os.getpid(),
            harness_session_id=session_id,
        ),
        debug=runtime_request.debug,
        ctx=resolved_context,
    )


def _build_background_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m meridian.lib.ops.spawn.execute_bg")
    parser.add_argument("--spawn-id", required=True)
    parser.add_argument("--project-root", required=True)
    return parser


def _background_worker_main(
    argv: Sequence[str] | None = None,
    *,
    ctx: RuntimeContext | None = None,
) -> int:
    resolved_context = runtime_context(ctx)
    parser = _build_background_worker_parser()
    parsed = parser.parse_args(list(argv) if argv is not None else None)

    project_root = Path(parsed.project_root).expanduser().resolve()
    prepared = prepare_for_runtime_write(project_root)
    project_paths = resolve_project_config_paths(project_root=prepared.project_root)
    spawn_id = SpawnId(parsed.spawn_id)
    if prepared.runtime_root is None:
        raise ValueError("Prepared runtime write context is missing runtime root.")
    runtime_root = prepared.runtime_root
    log_dir: Path | None = None
    try:
        install(
            TelemetryPlan(
                mode=TelemetryMode.SEGMENT,
                runtime_root=runtime_root,
                logical_owner=str(spawn_id),
            )
        )
        register_spawn_telemetry_observer()
        log_dir = resolve_spawn_log_dir(
            project_paths.project_root, spawn_id, runtime_root=runtime_root
        )

        try:
            launch_request = _load_bg_worker_request(log_dir)
        except Exception as exc:
            error = f"Failed to load background worker request: {exc}"
            _record_launch_boundary_observation(
                runtime_root,
                str(spawn_id),
                event=EVENT_WORKER_FAILURE,
                stage="load_worker_request",
                worker_pid=os.getpid(),
                error=error,
                exception=exc,
            )
            finalize_launch_failure_sync(
                runtime_root,
                project_root,
                spawn_id,
                error,
            )
            logger.error(
                "Failed to load background worker request.",
                spawn_id=str(spawn_id),
                log_dir=log_dir.as_posix(),
                exc_info=True,
            )
            return 1

        return asyncio.run(
            _execute_existing_spawn(
                spawn_id=spawn_id,
                project_paths=project_paths,
                launch_request=launch_request,
                ctx=resolved_context,
                prepared=prepared,
            )
        )
    except Exception as exc:
        _record_launch_boundary_observation(
            runtime_root,
            str(spawn_id),
            event=EVENT_WORKER_FAILURE,
            stage="worker_backstop",
            worker_pid=os.getpid(),
            error="background_worker_crash",
            exception=exc,
        )
        finalize_launch_failure_sync(
            runtime_root,
            project_root,
            spawn_id,
            "background_worker_crash",
        )
        logger.exception("Background worker crashed.", spawn_id=str(spawn_id))
        return 1
    finally:
        if log_dir is not None:
            _cleanup_background_runtime_artifacts(log_dir)


if __name__ == "__main__":
    raise SystemExit(_background_worker_main())


__all__ = [
    "_BACKGROUND_RUNTIME_ARTIFACTS",
    "BackgroundWorkerLaunchRequest",
    "_background_worker_main",
    "_build_background_worker_command",
    "_cleanup_background_runtime_artifacts",
    "_load_bg_worker_request",
    "_persist_bg_worker_request",
    "_record_launch_boundary_observation",
]
