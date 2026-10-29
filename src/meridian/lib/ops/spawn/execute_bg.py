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
from meridian.lib.state.launch_lock import LaunchSlot, adopt_handoff, take_handoff_fd
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.telemetry.bootstrap import TelemetryMode, TelemetryPlan, install
from meridian.lib.telemetry.init import setup_telemetry
from meridian.lib.telemetry.observer import register_spawn_telemetry_observer

from ..runtime import OperationRuntime, runtime_context
from .execute_init import ParamsRequestMetadata
from .execute_runner import launch_prepared_spawn
from .failure_policy import finalize_launch_failure, finalize_launch_failure_sync

logger = structlog.get_logger(__name__)
_SETUP_TELEMETRY_COMPAT = setup_telemetry


class BackgroundWorkerLaunchRequest(BaseModel):
    """Background worker request payload persisted to disk."""

    model_config = ConfigDict(frozen=True)

    request: SpawnRequest
    runtime: LaunchRuntime
    request_metadata: ParamsRequestMetadata


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


def _cleanup_background_runtime_artifacts(launch: LaunchPaths) -> None:
    """Remove non-durable launcher artifacts after terminal completion."""
    for target in (launch.background_stdout, launch.background_stderr, launch.bg_worker_request):
        with suppress(FileNotFoundError):
            target.unlink()


def _persist_bg_worker_request(launch: LaunchPaths, payload: BackgroundWorkerLaunchRequest) -> None:
    """Write background worker launch request to the launch's files."""

    atomic_write_text(launch.bg_worker_request, payload.model_dump_json(indent=2) + "\n")


def _load_bg_worker_request(launch: LaunchPaths) -> BackgroundWorkerLaunchRequest:
    """Load background worker launch request from the launch's files."""

    return BackgroundWorkerLaunchRequest.model_validate_json(
        launch.bg_worker_request.read_text(encoding="utf-8")
    )


def _build_background_worker_command(
    *,
    runtime_root: Path,
    spawn_id: SpawnId,
    project_paths: Any,
) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "meridian.lib.ops.spawn.execute_bg",
        "--spawn-id",
        str(spawn_id),
        "--project-root",
        project_paths.project_root.as_posix(),
        "--runtime-root",
        str(runtime_root),
    )


async def _execute_existing_spawn(
    *,
    launch: LaunchPaths,
    project_paths: Any,
    launch_request: BackgroundWorkerLaunchRequest,
    sink: Any | None = None,
    ctx: RuntimeContext | None = None,
    prepared: RuntimeWriteContext,
    runtime_root: Path,
    launch_slot: LaunchSlot,
) -> int:
    spawn_id = launch.spawn_id
    resolved_context = runtime_context(ctx)
    runtime = OperationRuntime.from_prepared(
        prepared,
        harness_registry=get_default_harness_registry(),
        sink=sink,
    )
    spawn_record = spawn_store.get_spawn(runtime_root, spawn_id)
    if spawn_record is None:
        logger.error("Spawn not found for background execution.", spawn_id=str(spawn_id))
        return 1

    request = launch_request.request
    runtime_request = launch_request.runtime
    resolved_model = (request.model or "").strip()
    resolved_harness_id = (request.harness or "").strip()
    if not resolved_harness_id:
        await finalize_launch_failure(
            runtime_root,
            project_paths.project_root,
            spawn_id,
            "Missing harness",
            launch=launch,
        )
        return 1

    spawn_status: SpawnStatus = (
        spawn_record.status if spawn_record.status != "unknown" else SpawnStatus.RUNNING
    )
    spawn = Spawn(
        spawn_id=SpawnId(spawn_record.id),
        prompt=request.prompt,
        model=ModelId(resolved_model),
        status=spawn_status,
    )

    resolved_execution_cwd = _resolve_background_execution_cwd(
        runtime_request=runtime_request,
        project_root=project_paths.project_root,
        work_id=spawn_record.work_id,
    )

    return await launch_prepared_spawn(
        spawn=spawn,
        launch=launch,
        request=request.model_copy(
            update={
                "model": resolved_model,
                "harness": resolved_harness_id,
                "agent": request.agent if request.agent is not None else spawn_record.agent,
                "skill_paths": spawn_record.skill_paths,
            }
        ),
        runtime_request=runtime_request,
        runtime=runtime,
        runtime_root=runtime_root,
        project_paths=project_paths,
        request_metadata=launch_request.request_metadata,
        spawn_record=spawn_record,
        execution_cwd=resolved_execution_cwd,
        work_id=spawn_record.work_id,
        debug=runtime_request.debug,
        ctx=resolved_context,
        launch_slot=launch_slot,
    )


def _build_background_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m meridian.lib.ops.spawn.execute_bg")
    parser.add_argument("--spawn-id", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    return parser


def _background_worker_main(
    argv: Sequence[str] | None = None,
    *,
    ctx: RuntimeContext | None = None,
) -> int:
    handoff_fd = take_handoff_fd()
    parser = _build_background_worker_parser()
    parsed = parser.parse_args(list(argv) if argv is not None else None)

    project_root = Path(parsed.project_root).expanduser().resolve()
    spawn_id = SpawnId(parsed.spawn_id)
    runtime_root = Path(parsed.runtime_root).expanduser().resolve()
    # Bootstrap and child context derive their runtime from the argv authority.
    os.environ["_MERIDIAN_RUNTIME_DIR"] = str(runtime_root)
    resolved_context = runtime_context(ctx)
    launch_lock = adopt_handoff(handoff_fd, runtime_root, spawn_id)
    with launch_lock:
        prepared = prepare_for_runtime_write(project_root)
        project_paths = resolve_project_config_paths(project_root=prepared.project_root)
        launch: LaunchPaths | None = None
        try:
            install(
                TelemetryPlan(
                    mode=TelemetryMode.SEGMENT,
                    runtime_root=runtime_root,
                    logical_owner=str(spawn_id),
                )
            )
            register_spawn_telemetry_observer()
            launch = spawn_store.read_launch_paths(runtime_root, spawn_id)

            try:
                launch_request = _load_bg_worker_request(launch)
            except Exception as exc:
                error = f"Failed to load background worker request: {exc}"
                finalize_launch_failure_sync(
                    runtime_root,
                    project_root,
                    spawn_id,
                    error,
                    launch=launch,
                )
                logger.error(
                    "Failed to load background worker request.",
                    spawn_id=str(spawn_id),
                    log_dir=launch.dir.as_posix(),
                    exc_info=True,
                )
                return 1

            return asyncio.run(
                _execute_existing_spawn(
                    launch=launch,
                    project_paths=project_paths,
                    launch_request=launch_request,
                    ctx=resolved_context,
                    prepared=prepared,
                    runtime_root=runtime_root,
                    launch_slot=launch_lock,
                )
            )
        except Exception:
            finalize_launch_failure_sync(
                runtime_root,
                project_root,
                spawn_id,
                "background_worker_crash",
                launch=launch,
            )
            logger.exception("Background worker crashed.", spawn_id=str(spawn_id))
            return 1
        finally:
            if launch is not None:
                _cleanup_background_runtime_artifacts(launch)


if __name__ == "__main__":
    raise SystemExit(_background_worker_main())


__all__ = [
    "BackgroundWorkerLaunchRequest",
    "_background_worker_main",
    "_build_background_worker_command",
    "_cleanup_background_runtime_artifacts",
    "_load_bg_worker_request",
    "_persist_bg_worker_request",
]
