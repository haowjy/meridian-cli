"""Spawn execution helpers shared by sync and async spawn handlers."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, cast

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.bootstrap.services import RuntimeWriteContext, prepare_for_runtime_write
from meridian.lib.config.project_paths import ProjectConfigPaths, resolve_project_config_paths
from meridian.lib.core.child_env import build_child_env_overrides
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.depth import current_meridian_depth, max_depth_reached
from meridian.lib.core.domain import Spawn, SpawnStatus
from meridian.lib.core.lifecycle import SpawnLifecycleService, create_lifecycle_service
from meridian.lib.core.sink import OutputSink
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.adapter import StreamEvent
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.artifact_io import write_projection_artifacts
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.cwd import resolve_child_execution_cwd
from meridian.lib.launch.fork import materialize_fork
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.session_scope import session_scope
from meridian.lib.launch.streaming_runner import execute_with_streaming
from meridian.lib.ops.work_attachment import ensure_explicit_work_item
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.state import spawn_store
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import (
    resolve_project_paths,
    resolve_spawn_log_dir,
    resolve_work_scratch_dir,
)
from meridian.lib.state.session_store import get_session_active_work_id, update_session_work_id
from meridian.lib.state.spawn_store import (
    BACKGROUND_LAUNCH_MODE,
    FOREGROUND_LAUNCH_MODE,
    LaunchMode,
)
from meridian.lib.telemetry.init import setup_telemetry
from meridian.lib.telemetry.observer import register_spawn_telemetry_observer

from ..runtime import (
    OperationRuntime,
    build_runtime,
    resolve_chat_id,
    resolve_runtime_root,
    runtime_context,
)
from .models import SpawnActionOutput, SpawnCreateInput
from .query import read_spawn_row

logger = structlog.get_logger(__name__)
_SETUP_TELEMETRY_COMPAT = setup_telemetry
_BACKGROUND_SUBMIT_MESSAGE = "Background spawn submitted."
_BACKGROUND_STDOUT_FILENAME = "background-launcher.stdout.log"
_BACKGROUND_STDERR_FILENAME = "background-launcher.stderr.log"
_BG_WORKER_REQUEST_FILENAME = "bg-worker-request.json"
_BACKGROUND_RUNTIME_ARTIFACTS = (
    _BACKGROUND_STDOUT_FILENAME,
    _BACKGROUND_STDERR_FILENAME,
    _BG_WORKER_REQUEST_FILENAME,
)


def _complete_spawn_sync(
    *,
    runtime_root: Path,
    lifecycle_service: SpawnLifecycleService,
    spawn_id: SpawnId,
    status: str,
    exit_code: int,
    origin: str,
    **metrics: Any,
) -> bool:
    """Run the async shared finalization seam from synchronous launch paths."""

    return asyncio.run(
        SpawnApplicationService(runtime_root, lifecycle_service).complete_spawn(
            spawn_id,
            status,
            exit_code,
            origin=origin,
            **metrics,
        )
    )


class _SpawnContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn: Spawn
    runtime_root: Path
    current_depth: int
    work_id: str | None = None


class _SessionExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: str
    work_id: str | None = None
    resolved_agent_name: str | None
    harness_session_id_observer: Callable[[str], None]


class BackgroundWorkerLaunchRequest(BaseModel):
    """Background worker request payload persisted to disk."""

    model_config = ConfigDict(frozen=True)

    request: SpawnRequest
    runtime: LaunchRuntime


def _cleanup_background_runtime_artifacts(log_dir: Path) -> None:
    """Remove non-durable launcher artifacts after terminal completion."""
    for name in _BACKGROUND_RUNTIME_ARTIFACTS:
        target = log_dir / name
        with suppress(FileNotFoundError):
            target.unlink()


def depth_limits(max_depth: int, *, ctx: RuntimeContext | None = None) -> tuple[int, int]:
    current_depth = runtime_context(ctx).depth
    max_depth_reached(current_depth, max_depth)
    return current_depth, max_depth


def _emit_subrun_event(
    payload: dict[str, Any],
    *,
    sink: OutputSink,
    ctx: RuntimeContext | None = None,
) -> None:
    resolved_context = runtime_context(ctx)
    if not resolved_context.is_nested:
        return
    event_payload = dict(payload)
    event_payload["v"] = 1
    parent_id = str(resolved_context.spawn_id or "")
    event_payload["parent"] = parent_id or None
    event_payload["ts"] = time.time()
    sink.event(event_payload)


def depth_exceeded_output(current_depth: int, max_depth: int) -> SpawnActionOutput:
    return SpawnActionOutput(
        command="spawn.create",
        status="failed",
        message=f"Max agent depth ({max_depth}) reached. Complete this task directly.",
        error="max_depth_exceeded",
        current_depth=current_depth,
        max_depth=max_depth,
    )


def _spawn_child_env(
    spawn_id: str | None = None,
    *,
    work_id: str | None = None,
    runtime_root: Path | None = None,
    autocompact: int | None = None,
    ctx: RuntimeContext | None = None,
) -> dict[str, str]:
    _ = spawn_id, work_id, runtime_root, ctx
    child_env: dict[str, str] = {}
    # K5 boundary: ChildEnvContext.child_context() in launch/context.py is the sole
    # producer of MERIDIAN_* child overrides. Plan overrides stay non-MERIDIAN.
    if autocompact is not None:
        child_env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(autocompact)
    return child_env


def _spawn_background_worker_env(
    *,
    project_root: Path,
    work_id: str | None = None,
    autocompact: int | None = None,
) -> dict[str, str]:
    """Build child env overrides for the detached background worker process.

    The background worker runs as a peer of the launching process (same
    ``MERIDIAN_DEPTH``), not as a depth-child.  Depth is inherited unchanged;
    only the work-item keys differ from the parent environment.
    """
    # Read the current depth so the worker inherits the same value.
    parent_depth = current_meridian_depth()

    normalized_work_id = (work_id or "").strip() or None
    work_dir: Path | None = None
    if normalized_work_id:
        work_dir = resolve_work_scratch_dir(
            resolve_project_paths(project_root).root_dir,
            normalized_work_id,
        )

    # Omit project_root/runtime_root/chat_id (pass None) — those are already
    # correct in the inherited os.environ.  increment_depth=False because the
    # background worker is a peer, not a depth-child.
    child_env = build_child_env_overrides(
        parent_spawn_id=None,  # inherited from os.environ
        project_root=None,
        runtime_root=None,
        parent_chat_id=None,
        parent_depth=parent_depth,
        work_id=normalized_work_id,
        work_dir=work_dir,
        increment_depth=False,
    )
    if autocompact is not None:
        child_env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(autocompact)
    return child_env



def _build_detached_popen_kwargs() -> dict[str, Any]:
    """Build platform-appropriate kwargs for a detached subprocess.Popen.

    On POSIX, uses start_new_session=True to create a new process group.
    On Windows, uses creationflags to detach the process.
    """
    if IS_WINDOWS:
        create_new_process_group = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0))
        return {
            "creationflags": create_new_process_group | detached_process,
        }
    return {
        "start_new_session": True,
        "close_fds": True,
    }


def _resolve_work_id(
    *,
    payload: SpawnCreateInput,
    runtime_context: RuntimeContext,
    work_id: str | None = None,
) -> str | None:
    requested_work_id = (work_id or payload.work).strip()
    if requested_work_id:
        return requested_work_id
    inherited_work_id = (runtime_context.work_id or "").strip()
    return inherited_work_id or None


def _init_spawn(
    *,
    payload: SpawnCreateInput,
    request: SpawnRequest,
    runtime: OperationRuntime,
    desc: str | None = None,
    work_id: str | None = None,
    status: SpawnStatus = "running",
    launch_mode: LaunchMode | None = None,
    runner_pid: int | None = None,
    execution_cwd: str | None = None,
    ctx: RuntimeContext | None = None,
) -> _SpawnContext:
    resolved_context = runtime_context(ctx)
    project_paths = resolve_project_config_paths(project_root=runtime.project_root)
    project_local_root = resolve_project_paths(project_paths.project_root).root_dir
    runtime_root = resolve_runtime_root(project_paths.project_root)
    resolved_work_id = _resolve_work_id(
        payload=payload,
        runtime_context=resolved_context,
        work_id=work_id,
    )
    if (payload.work or "").strip():
        resolved_work_id = cast("str", resolved_work_id)
        resolved_work_id = ensure_explicit_work_item(project_local_root, resolved_work_id)
    resolved_desc = (desc if desc is not None else payload.desc).strip() or None
    service = create_lifecycle_service(project_paths.project_root, runtime_root)
    spawn_id = service.start(
        chat_id=resolve_chat_id(ctx=resolved_context, fallback="c0"),
        parent_id=str(resolved_context.spawn_id) if resolved_context.spawn_id else None,
        model=request.model or "",
        agent=request.agent or "",
        agent_path=request.agent_metadata.get("session_agent_path") or None,
        skills=request.skills,
        skill_paths=request.skill_paths,
        harness=request.harness or "",
        kind="child",
        prompt=request.prompt,
        desc=resolved_desc,
        work_id=resolved_work_id,
        # I-10: do NOT pre-populate harness_session_id on fork starts.
        # materialize_fork() writes it via update_spawn after the row exists.
        harness_session_id=(
            None
            if request.session.continue_fork
            else request.session.requested_harness_session_id
        ),
        execution_cwd=execution_cwd,
        launch_mode=launch_mode,
        runner_pid=runner_pid,
        status=status,
    )
    spawn = Spawn(
        spawn_id=SpawnId(spawn_id),
        prompt=request.prompt,
        model=ModelId(request.model or ""),
        status=status,
    )
    current_depth = resolved_context.depth
    run_start_event: dict[str, Any] = {
        "t": "meridian.spawn.start",
        "id": str(spawn.spawn_id),
        "model": request.model or "",
        "d": current_depth,
    }
    if request.agent is not None:
        run_start_event["agent"] = request.agent
    _emit_subrun_event(run_start_event, sink=runtime.sink, ctx=resolved_context)
    return _SpawnContext(
        spawn=spawn,
        runtime_root=runtime_root,
        current_depth=current_depth,
        work_id=resolved_work_id,
    )


def _write_params_json(
    project_paths: ProjectConfigPaths,
    spawn_id: SpawnId,
    request: SpawnRequest,
    *,
    desc: str = "",
    work_id: str | None = None,
) -> None:
    """Write resolved execution params to the spawn directory."""
    params_path = resolve_spawn_log_dir(project_paths.project_root, spawn_id) / "params.json"
    params_path.parent.mkdir(parents=True, exist_ok=True)
    params_payload = {
        "model": request.model or "",
        "harness": request.harness or "",
        "agent": request.agent,
        "agent_path": request.agent_metadata.get("session_agent_path") or "",
        "adhoc_agent_payload": request.agent_metadata.get("adhoc_agent_payload") or "",
        "desc": desc,
        "work_id": work_id,
        "prompt_length": len(request.prompt),
        "reference_files": list(request.reference_files),
        "template_vars": request.template_vars,
        "skills": list(request.skills),
        "skill_paths": list(request.skill_paths),
        "continue_session": request.session.requested_harness_session_id,
        "continue_fork": request.session.continue_fork,
        "forked_from_chat_id": request.session.forked_from_chat_id,
    }
    atomic_write_text(params_path, json.dumps(params_payload, indent=2) + "\n")


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


def _resolve_session_continuation(
    *,
    request: SpawnRequest,
    harness_id: HarnessId,
    harness_adapter: Any,
) -> SessionRequest:
    requested_harness_session_id = (
        (request.session.requested_harness_session_id or "").strip() or None
    )
    requested_continue_fork = request.session.continue_fork
    requested_harness = (request.session.continue_harness or "").strip()
    if request.session.continue_source_tracked and requested_harness_session_id is None:
        raise ValueError(
            "Source reference has no recorded harness session — cannot continue/fork."
        )

    resolved_continue_harness_session_id: str | None = None
    resolved_continue_fork = False
    if requested_harness_session_id:
        if (
            requested_harness and requested_harness != str(harness_id)
        ) or not harness_adapter.capabilities.supports_session_resume:
            resolved_continue_harness_session_id = None
        else:
            resolved_continue_harness_session_id = requested_harness_session_id
            if requested_continue_fork:
                if harness_adapter.capabilities.supports_session_fork:
                    resolved_continue_fork = True
                else:
                    resolved_continue_fork = False

    # I-10: fork materialization is deferred to the calling executor, which
    # calls materialize_fork() via the sole owner in launch/fork.py, after both
    # the spawn row and chat row exist.  resolved_continue_fork=True is preserved
    # here so the executor knows a fork is needed.

    return SessionRequest(
        requested_harness_session_id=resolved_continue_harness_session_id,
        continue_harness=request.session.continue_harness,
        continue_source_tracked=request.session.continue_source_tracked,
        continue_source_ref=request.session.continue_source_ref,
        continue_chat_id=request.session.continue_chat_id,
        continue_fork=resolved_continue_fork,
        forked_from_chat_id=request.session.forked_from_chat_id,
        source_execution_cwd=request.session.source_execution_cwd,
        source_claude_config_dir=request.session.source_claude_config_dir,
    )


@contextmanager
def _session_execution_context(
    *,
    runtime_root: Path,
    harness_id: str,
    harness_session_id: str,
    model: str,
    session_agent: str,
    session_agent_path: str,
    skills: tuple[str, ...],
    session_skill_paths: tuple[str, ...],
    run_agent_name: str | None,
    inherited_work_id: str | None = None,
    forked_from_chat_id: str | None = None,
    execution_cwd: str | None = None,
) -> Iterator[_SessionExecutionContext]:
    with session_scope(
        runtime_root=runtime_root,
        harness=harness_id,
        harness_session_id=harness_session_id,
        model=model,
        agent=session_agent,
        agent_path=session_agent_path,
        skills=skills,
        skill_paths=session_skill_paths,
        forked_from_chat_id=forked_from_chat_id,
        execution_cwd=execution_cwd,
    ) as managed:
        attached_work_id = get_session_active_work_id(runtime_root, managed.chat_id)
        if attached_work_id is None:
            attached_work_id = (inherited_work_id or "").strip() or None
            if attached_work_id is not None:
                update_session_work_id(runtime_root, managed.chat_id, attached_work_id)
        yield _SessionExecutionContext(
            chat_id=managed.chat_id,
            work_id=attached_work_id,
            resolved_agent_name=run_agent_name,
            harness_session_id_observer=managed.record_harness_session_id,
        )


async def launch_prepared_spawn(
    *,
    spawn: Spawn,
    request: SpawnRequest,
    runtime_request: LaunchRuntime,
    runtime: OperationRuntime,
    runtime_root: Path,
    project_paths: ProjectConfigPaths,
    spawn_record: spawn_store.SpawnRecord | None = None,
    execution_cwd: str,
    work_id: str | None = None,
    autocompact: int | None = None,
    stream_stdout_to_terminal: bool = False,
    stream_stderr_to_terminal: bool = False,
    event_observer: Callable[[StreamEvent], None] | None = None,
    debug: bool = False,
    ctx: RuntimeContext | None = None,
) -> int:
    """Shared post-row, pre-run launch handoff for foreground/background spawns.

    Owns launch_failure finalization for exceptions before runner entry.
    After runner entry, execute_with_streaming owns terminal finalization.
    """

    resolved_context = runtime_context(ctx)
    lifecycle_service = create_lifecycle_service(project_paths.project_root, runtime_root)
    try:
        harness_id = HarnessId(request.harness or "")
        harness_adapter = runtime.harness_registry.get_subprocess_harness(harness_id)
        resolved_session = _resolve_session_continuation(
            request=request,
            harness_id=harness_id,
            harness_adapter=harness_adapter,
        )

        resolved_request = request.model_copy(update={"session": resolved_session})
        resolved_agent_name = request.agent
        if spawn_record is not None:
            resolved_agent_name = (
                request.agent if request.agent is not None else spawn_record.agent
            )

        with _session_execution_context(
            runtime_root=runtime_root,
            harness_id=request.harness or "",
            harness_session_id=(
                resolved_session.requested_harness_session_id
                or (spawn_record.harness_session_id if spawn_record else "")
                or ""
            ),
            model=request.model or "",
            session_agent=(
                (spawn_record.agent if spawn_record else "")
                or request.agent_metadata.get("session_agent", "")
            ),
            session_agent_path=(
                (spawn_record.agent_path if spawn_record else "")
                or request.agent_metadata.get("session_agent_path", "")
            ),
            skills=request.skills or (spawn_record.skills if spawn_record else ()),
            session_skill_paths=(
                spawn_record.skill_paths if spawn_record else request.skill_paths
            ),
            run_agent_name=resolved_agent_name,
            inherited_work_id=work_id,
            forked_from_chat_id=resolved_session.forked_from_chat_id,
            execution_cwd=execution_cwd,
        ) as session_context:
            resolved_request = resolved_request.model_copy(
                update={"agent": session_context.resolved_agent_name}
            )
            # I-10/I-11: spawn row AND chat row now both exist. Materialize any
            # pending fork via the sole owner so the spawn row receives the forked
            # session ID via update_spawn (not pre-populated on the start row).
            if resolved_session.continue_fork and resolved_session.requested_harness_session_id:
                forked_session_id = materialize_fork(
                    adapter=harness_adapter,
                    source_session_id=resolved_session.requested_harness_session_id,
                    runtime_root=runtime_root,
                    spawn_id=spawn.spawn_id,
                )
                resolved_request = resolved_request.model_copy(
                    update={
                        "session": resolved_request.session.model_copy(
                            update={
                                "requested_harness_session_id": forked_session_id,
                                "continue_fork": False,
                            }
                        )
                    }
                )

            run_env_overrides = _spawn_child_env(
                str(spawn.spawn_id),
                work_id=session_context.work_id or work_id,
                runtime_root=runtime_root,
                autocompact=autocompact,
                ctx=resolved_context,
            )
            runtime_work_id = session_context.work_id or work_id
            final_request = resolved_request.model_copy(
                update={"work_id_hint": runtime_work_id}
            )
            launch_runtime = runtime_request.model_copy(
                update={
                    "argv_intent": LaunchArgvIntent.SPEC_ONLY,
                    "runtime_root": runtime_root.as_posix(),
                    "project_paths_project_root": project_paths.project_root.as_posix(),
                    "project_paths_execution_cwd": execution_cwd,
                }
            )
            launch_context = build_launch_context(
                spawn_id=str(spawn.spawn_id),
                request=final_request,
                runtime=launch_runtime,
                harness_registry=runtime.harness_registry,
                plan_overrides=run_env_overrides,
                runtime_work_id=runtime_work_id,
            )
            write_projection_artifacts(
                log_dir=resolve_spawn_log_dir(project_paths.project_root, spawn.spawn_id),
                launch_context=launch_context,
                surface="spawn",
            )

            # Hand off to runner — runner owns finalization from here.
            return await execute_with_streaming(
                spawn,
                request=final_request,
                launch_context=launch_context,
                project_root=project_paths.project_root,
                runtime_root=runtime_root,
                artifacts=runtime.artifacts,
                harness_session_id_observer=session_context.harness_session_id_observer,
                event_observer=event_observer,
                stream_stdout_to_terminal=stream_stdout_to_terminal,
                stream_stderr_to_terminal=stream_stderr_to_terminal,
                debug=debug,
            )
    except Exception as exc:
        # Pre-run failure: this helper owns launch_failure finalization.
        await SpawnApplicationService(runtime_root, lifecycle_service).complete_spawn(
            spawn.spawn_id,
            "failed",
            1,
            origin="launch_failure",
            error=str(exc),
        )
        logger.exception("Pre-launch setup failed.", spawn_id=str(spawn.spawn_id))
        return 1


async def _execute_existing_spawn(
    *,
    spawn_id: SpawnId,
    project_paths: ProjectConfigPaths,
    launch_request: BackgroundWorkerLaunchRequest,
    sink: OutputSink | None = None,
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

    lifecycle_service = create_lifecycle_service(project_paths.project_root, runtime_root)

    request = launch_request.request
    runtime_request = launch_request.runtime
    resolved_model = (request.model or "").strip()
    resolved_harness_id = (request.harness or "").strip()
    resolved_prompt = (request.prompt or "").strip()
    if not resolved_prompt:
        await SpawnApplicationService(runtime_root, lifecycle_service).complete_spawn(
            spawn_id,
            "failed",
            1,
            origin="launch_failure",
            error="Missing prompt",
        )
        return 1
    if not resolved_harness_id:
        await SpawnApplicationService(runtime_root, lifecycle_service).complete_spawn(
            spawn_id,
            "failed",
            1,
            origin="launch_failure",
            error="Missing harness",
        )
        return 1

    spawn_status: SpawnStatus = (
        spawn_record.status if spawn_record.status != "unknown" else "queued"
    )
    spawn = Spawn(
        spawn_id=SpawnId(spawn_record.id),
        prompt=resolved_prompt,
        model=ModelId(resolved_model),
        status=spawn_status,
    )

    resolved_execution_cwd = (
        (runtime_request.project_paths_execution_cwd or "").strip() or None
    )
    if not resolved_execution_cwd:
        resolved_execution_cwd = str(
            resolve_child_execution_cwd(
                project_root=project_paths.project_root,
                spawn_id=str(spawn_id),
                harness_id=resolved_harness_id,
            )
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
        autocompact=request.autocompact,
        debug=runtime_request.debug,
        ctx=resolved_context,
    )

def _build_background_worker_command(
    *,
    spawn_id: str,
    project_paths: ProjectConfigPaths,
) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "meridian.lib.ops.spawn.execute",
        "--spawn-id",
        spawn_id,
        "--project-root",
        project_paths.project_root.as_posix(),
    )


def execute_spawn_background(
    *,
    payload: SpawnCreateInput,
    request: SpawnRequest,
    runtime: OperationRuntime,
    ctx: RuntimeContext | None = None,
) -> SpawnActionOutput:
    resolved_context = runtime_context(ctx)
    project_paths = resolve_project_config_paths(project_root=runtime.project_root)
    if payload.stream:
        logger.warning("--stream requires --foreground; output goes to spawn log files.")
    autocompact = request.autocompact
    context = _init_spawn(
        payload=payload,
        request=request,
        runtime=runtime,
        desc=payload.desc,
        work_id=payload.work,
        status="queued",
        launch_mode=BACKGROUND_LAUNCH_MODE,
        execution_cwd=str(project_paths.execution_cwd),
        ctx=resolved_context,
    )
    spawn_id_text = str(context.spawn.spawn_id)
    execution_cwd_str = str(
        resolve_child_execution_cwd(
            project_root=project_paths.project_root,
            spawn_id=spawn_id_text,
            harness_id=request.harness or "",
        )
    )
    # Record pre-computed execution_cwd immediately so it's correct even if
    # the background worker dies before runner.py's authoritative update.
    if execution_cwd_str != str(project_paths.project_root):
        spawn_store.update_spawn(
            context.runtime_root,
            context.spawn.spawn_id,
            execution_cwd=execution_cwd_str,
        )
    log_dir = resolve_spawn_log_dir(project_paths.project_root, context.spawn.spawn_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    lifecycle_service = create_lifecycle_service(project_paths.project_root, context.runtime_root)
    try:
        _write_params_json(
            project_paths,
            context.spawn.spawn_id,
            request,
            desc=payload.desc,
            work_id=context.work_id,
        )
    except Exception:
        logger.warning("Failed to write params.json", spawn_id=spawn_id_text, exc_info=True)

    warning = request.warning
    context_from_resolved = request.context_from
    try:
        persisted_request = request.model_copy(update={"work_id_hint": context.work_id})
        launch_runtime = LaunchRuntime(
            argv_intent=LaunchArgvIntent.SPEC_ONLY,
            debug=payload.debug,
            runtime_root=context.runtime_root.as_posix(),
            project_paths_project_root=project_paths.project_root.as_posix(),
            project_paths_execution_cwd=execution_cwd_str,
        )
        _persist_bg_worker_request(
            log_dir,
            BackgroundWorkerLaunchRequest(
                request=persisted_request,
                runtime=launch_runtime,
            ),
        )
    except Exception as exc:
        _complete_spawn_sync(
            runtime_root=context.runtime_root,
            lifecycle_service=lifecycle_service,
            spawn_id=context.spawn.spawn_id,
            status="failed",
            exit_code=1,
            origin="launch_failure",
            error=str(exc),
        )
        _cleanup_background_runtime_artifacts(log_dir)
        logger.exception(
            "Failed to persist background worker params.",
            spawn_id=spawn_id_text,
        )
        return SpawnActionOutput(
            command="spawn.create",
            status="failed",
            spawn_id=spawn_id_text,
            message=f"Failed to launch background spawn: {exc}",
            error="background_launch_failed",
            model=request.model or "",
            harness_id=request.harness or "",
            warning=warning,
            agent=request.agent,
            reference_files=request.reference_files,
            template_vars=request.template_vars,
            context_from_resolved=context_from_resolved,
            exit_code=1,
        )

    launch_command = _build_background_worker_command(
        spawn_id=spawn_id_text,
        project_paths=project_paths,
    )
    stdout_path = log_dir / _BACKGROUND_STDOUT_FILENAME
    stderr_path = log_dir / _BACKGROUND_STDERR_FILENAME

    launch_env = dict(os.environ)
    launch_env.update(
        _spawn_background_worker_env(
            project_root=project_paths.project_root,
            work_id=context.work_id,
            autocompact=autocompact,
        )
    )
    try:
        with (
            stdout_path.open("ab") as stdout_handle,
            stderr_path.open("ab") as stderr_handle,
        ):
            process = subprocess.Popen(
                launch_command,
                cwd=project_paths.execution_cwd,
                env=launch_env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                **_build_detached_popen_kwargs(),
            )
    except OSError as exc:
        _complete_spawn_sync(
            runtime_root=context.runtime_root,
            lifecycle_service=lifecycle_service,
            spawn_id=context.spawn.spawn_id,
            status="failed",
            exit_code=1,
            origin="launch_failure",
            error=str(exc),
        )
        _cleanup_background_runtime_artifacts(log_dir)
        logger.exception(
            "Failed to launch background spawn worker.",
            spawn_id=spawn_id_text,
            command=list(launch_command),
        )
        return SpawnActionOutput(
            command="spawn.create",
            status="failed",
            spawn_id=spawn_id_text,
            message=f"Failed to launch background spawn: {exc}",
            error="background_launch_failed",
            model=request.model or "",
            harness_id=request.harness or "",
            warning=warning,
            agent=request.agent,
            reference_files=request.reference_files,
            template_vars=request.template_vars,
            context_from_resolved=context_from_resolved,
            exit_code=1,
        )

    lifecycle_service.mark_running(
        context.spawn.spawn_id,
        launch_mode=BACKGROUND_LAUNCH_MODE,
        runner_pid=process.pid,
    )
    # The Popen object goes out of scope without wait(). This is intentional:
    # the child spawns in its own session (start_new_session=True) and is
    # re-parented to init/systemd. We only need the PID for diagnostics.
    return SpawnActionOutput(
        command="spawn.create",
        status="running",
        spawn_id=spawn_id_text,
        message=_BACKGROUND_SUBMIT_MESSAGE,
        model=request.model or "",
        harness_id=request.harness or "",
        warning=warning,
        agent=request.agent,
        reference_files=request.reference_files,
        template_vars=request.template_vars,
        context_from_resolved=context_from_resolved,
        background=True,
    )


def execute_spawn_blocking(
    *,
    payload: SpawnCreateInput,
    request: SpawnRequest,
    runtime: OperationRuntime,
    ctx: RuntimeContext | None = None,
) -> SpawnActionOutput:
    resolved_context = runtime_context(ctx)
    project_paths = resolve_project_config_paths(project_root=runtime.project_root)
    autocompact = request.autocompact
    context = _init_spawn(
        payload=payload,
        request=request,
        runtime=runtime,
        desc=payload.desc,
        work_id=payload.work,
        status="queued",
        launch_mode=FOREGROUND_LAUNCH_MODE,
        runner_pid=os.getpid(),
        execution_cwd=str(project_paths.execution_cwd),
        ctx=resolved_context,
    )
    spawn = context.spawn

    warning = request.warning
    context_from_resolved = request.context_from

    try:
        execution_cwd_str = str(
            resolve_child_execution_cwd(
                project_root=project_paths.project_root,
                spawn_id=str(spawn.spawn_id),
                harness_id=request.harness or "",
            )
        )
        if execution_cwd_str != str(project_paths.project_root):
            # Pre-compute execution CWD for immediate visibility.
            # runner.py writes the authoritative value right before execution.
            spawn_store.update_spawn(
                context.runtime_root,
                spawn.spawn_id,
                execution_cwd=execution_cwd_str,
            )
        try:
            _write_params_json(
                project_paths,
                spawn.spawn_id,
                request,
                desc=payload.desc,
                work_id=context.work_id,
            )
        except Exception:
            logger.warning(
                "Failed to write params.json",
                spawn_id=str(spawn.spawn_id),
                exc_info=True,
            )
        # Emit spawn ID immediately so the caller can reference it while blocking.
        print(json.dumps({"spawn_id": str(spawn.spawn_id), "status": "running"}), flush=True)
        started = time.monotonic()
        stream_stdout_to_terminal = payload.stream
        event_observer = None
        # Spawn execution stays silent unless --stream is explicitly enabled.

        exit_code = asyncio.run(
            launch_prepared_spawn(
                spawn=spawn,
                request=request,
                runtime_request=LaunchRuntime(
                    argv_intent=LaunchArgvIntent.SPEC_ONLY,
                    debug=payload.debug,
                    runtime_root=context.runtime_root.as_posix(),
                    project_paths_project_root=project_paths.project_root.as_posix(),
                    project_paths_execution_cwd=project_paths.execution_cwd.as_posix(),
                ),
                runtime=runtime,
                runtime_root=context.runtime_root,
                project_paths=project_paths,
                execution_cwd=execution_cwd_str,
                work_id=context.work_id,
                autocompact=autocompact,
                stream_stdout_to_terminal=stream_stdout_to_terminal,
                stream_stderr_to_terminal=payload.stream,
                event_observer=event_observer,
                debug=payload.debug,
                ctx=resolved_context,
            )
        )
    except Exception as exc:
        lifecycle_service = create_lifecycle_service(
            project_paths.project_root,
            context.runtime_root,
        )
        _complete_spawn_sync(
            runtime_root=context.runtime_root,
            lifecycle_service=lifecycle_service,
            spawn_id=spawn.spawn_id,
            status="failed",
            exit_code=1,
            origin="launch_failure",
            error=str(exc),
        )
        logger.exception("Foreground spawn crashed.", spawn_id=str(spawn.spawn_id))
        return SpawnActionOutput(
            command="spawn.create",
            status="failed",
            spawn_id=str(spawn.spawn_id),
            message=f"Spawn execution failed: {exc}",
            error="execution_crash",
            model=request.model or "",
            harness_id=request.harness or "",
            warning=warning,
            agent=request.agent,
            reference_files=request.reference_files,
            template_vars=request.template_vars,
            context_from_resolved=context_from_resolved,
            exit_code=1,
        )

    duration = time.monotonic() - started
    row = read_spawn_row(project_paths.project_root, str(spawn.spawn_id))
    # Report is read on-demand via `spawn show`, not inlined here.
    status = "failed"
    if row is not None:
        status = row.status
    done_secs = duration
    tokens_total: int | None = None
    if row is not None:
        row_duration = row.duration_secs
        if row_duration is not None:
            done_secs = row_duration
        input_tokens = row.input_tokens
        output_tokens = row.output_tokens
        if input_tokens is not None and output_tokens is not None:
            tokens_total = input_tokens + output_tokens
    _emit_subrun_event(
        {
            "t": "meridian.spawn.done",
            "id": str(spawn.spawn_id),
            "exit": exit_code,
            "secs": done_secs,
            "tok": tokens_total,
            "d": context.current_depth,
        },
        sink=runtime.sink,
        ctx=resolved_context,
    )

    return SpawnActionOutput(
        command="spawn.create",
        status=status,
        spawn_id=str(spawn.spawn_id),
        message="Spawn completed.",
        model=request.model or "",
        harness_id=request.harness or "",
        warning=warning,
        agent=request.agent,
        reference_files=request.reference_files,
        template_vars=request.template_vars,
        context_from_resolved=context_from_resolved,
        report=None,
        exit_code=exit_code,
        duration_secs=duration,
    )

def _build_background_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m meridian.lib.ops.spawn.execute")
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
        from meridian.lib.telemetry.bootstrap import TelemetryMode, TelemetryPlan, install

        install(
            TelemetryPlan(
                mode=TelemetryMode.SEGMENT,
                runtime_root=runtime_root,
                logical_owner=str(spawn_id),
            )
        )
        register_spawn_telemetry_observer()
        log_dir = resolve_spawn_log_dir(project_paths.project_root, spawn_id)

        try:
            launch_request = _load_bg_worker_request(log_dir)
        except Exception as exc:
            error = f"Failed to load background worker request: {exc}"
            lifecycle_service = create_lifecycle_service(project_root, runtime_root)
            _complete_spawn_sync(
                runtime_root=runtime_root,
                lifecycle_service=lifecycle_service,
                spawn_id=spawn_id,
                status="failed",
                exit_code=1,
                origin="launch_failure",
                error=error,
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
    except Exception:
        lifecycle_service = create_lifecycle_service(project_root, runtime_root)
        _complete_spawn_sync(
            runtime_root=runtime_root,
            lifecycle_service=lifecycle_service,
            spawn_id=spawn_id,
            status="failed",
            exit_code=1,
            origin="launch_failure",
            error="background_worker_crash",
        )
        logger.exception("Background worker crashed.", spawn_id=str(spawn_id))
        return 1
    finally:
        if log_dir is not None:
            _cleanup_background_runtime_artifacts(log_dir)


if __name__ == "__main__":
    raise SystemExit(_background_worker_main())


__all__ = [
    "depth_exceeded_output",
    "depth_limits",
    "execute_spawn_background",
    "execute_spawn_blocking",
    "launch_prepared_spawn",
]
