"""Spawn execution helpers shared by sync and async spawn handlers."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.bootstrap.services import (
    RuntimeWriteContext,
    build_spawn_lifecycle_service_from_roots,
    prepare_for_runtime_write,
)
from meridian.lib.config.project_paths import ProjectConfigPaths, resolve_project_config_paths
from meridian.lib.core.child_env import build_child_env_overrides
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.depth import current_meridian_depth, max_depth_reached
from meridian.lib.core.domain import Spawn, SpawnStatus
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.sink import OutputSink
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.adapter import (
    ForkMaterializationMode,
    HarnessPrelaunchState,
    StreamEvent,
)
from meridian.lib.harness.claude_preflight import (
    MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV,
    cleanup_claude_overlay,
    ensure_claude_session_accessible,
    prepare_isolated_claude_config,
    resolve_claude_overlay_roots,
    resolve_overlay_materialization_canonical_root,
)
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.artifact_io import write_projection_artifacts
from meridian.lib.launch.claude_session_access import (
    resolve_claude_session_access_source,
)
from meridian.lib.launch.context import LaunchContext, build_launch_context
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
from meridian.lib.state.current_work import get_current_work_id
from meridian.lib.state.launch_boundary import (
    EVENT_HARNESS_SESSION_OBSERVED,
    EVENT_PARENT_LAUNCH_ATTEMPT,
    EVENT_PARENT_LAUNCH_FAILED,
    EVENT_PARENT_LAUNCH_SPAWNED,
    EVENT_WORKER_BOOT,
    EVENT_WORKER_FAILURE,
    EVENT_WORKER_REQUEST_LOADED,
    EVENT_WORKER_TAKEOVER_STARTED,
    record_launch_boundary_event,
)
from meridian.lib.state.paths import (
    resolve_project_paths,
    resolve_spawn_log_dir,
    resolve_work_scratch_dir_for_project,
)
from meridian.lib.state.session_store import (
    get_session_active_work_id,
    update_session_claude_config_dir,
    update_session_work_id,
)
from meridian.lib.state.spawn.model import (
    BACKGROUND_LAUNCH_MODE,
    FOREGROUND_LAUNCH_MODE,
    LaunchMode,
    SpawnRecord,
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
from .failure_policy import finalize_launch_failure, finalize_launch_failure_sync
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


@dataclass
class PreparedExecutionHandoff:
    """Result of successful spawn preparation. Owns session scope cleanup."""

    resolved_request: SpawnRequest
    launch_context: LaunchContext
    session_context: _SessionExecutionContext
    session_exit_stack: ExitStack
    execution_cwd: str
    work_id: str | None
    harness_session_id_observer: Callable[[str], None]


@dataclass(frozen=True)
class PreparedClaudeOverlay:
    """Claude overlay setup outputs used by seeding and cleanup."""

    isolated_config_root: Path | None
    effective_config_root: Path | None
    materialization_root: Path | None


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
        work_dir = resolve_work_scratch_dir_for_project(
            project_root,
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
    runtime_root: Path,
    work_id: str | None = None,
) -> str | None:
    requested_work_id = (work_id or payload.work).strip()
    if requested_work_id:
        return requested_work_id
    inherited_work_id = (runtime_context.work_id or "").strip()
    if inherited_work_id:
        return inherited_work_id
    return get_current_work_id(runtime_root)


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
        runtime_root=runtime_root,
        work_id=work_id,
    )
    if (payload.work or "").strip():
        resolved_work_id = cast("str", resolved_work_id)
        resolved_work_id = ensure_explicit_work_item(project_local_root, resolved_work_id)
    resolved_desc = (desc if desc is not None else payload.desc).strip() or None
    service = build_spawn_lifecycle_service_from_roots(project_paths.project_root, runtime_root)
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
        "adhoc_agent_payload": request.prompt_payload.adhoc_agent_payload,
        "appended_system_prompt": request.prompt_payload.appended_system_prompt,
        "user_turn_content": request.prompt_payload.user_turn_content,
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


async def _prepare_execution_handoff(
    *,
    spawn: Spawn,
    request: SpawnRequest,
    runtime_request: LaunchRuntime,
    runtime: OperationRuntime,
    runtime_root: Path,
    project_paths: ProjectConfigPaths,
    spawn_record: SpawnRecord | None,
    execution_cwd: str,
    work_id: str | None,
    autocompact: int | None,
    ctx: RuntimeContext | None,
) -> PreparedExecutionHandoff:
    """Prepare execution context; close session scope before re-raising on failure."""

    resolved_context = runtime_context(ctx)
    local_stack = ExitStack()
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

        session_context = local_stack.enter_context(
            _session_execution_context(
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
            )
        )
        resolved_request = resolved_request.model_copy(
            update={"agent": session_context.resolved_agent_name}
        )
        # I-10/I-11: spawn row AND chat row now both exist. Harnesses that
        # declare MERIDIAN_MATERIALIZED_FORK fork here so the spawn row
        # receives the forked session ID via update_spawn (not pre-populated on
        # the start row). Other harnesses keep continue_fork native for launch
        # projection.
        if (
            harness_adapter.contract.bootstrap.fork_materialization
            is ForkMaterializationMode.MERIDIAN_MATERIALIZED_FORK
            and resolved_session.continue_fork
            and resolved_session.requested_harness_session_id
        ):
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
        final_request = resolved_request.model_copy(update={"work_id_hint": runtime_work_id})
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

        handoff_stack = local_stack
        local_stack = ExitStack()
        return PreparedExecutionHandoff(
            resolved_request=final_request,
            launch_context=launch_context,
            session_context=session_context,
            session_exit_stack=handoff_stack,
            execution_cwd=execution_cwd,
            work_id=runtime_work_id,
            harness_session_id_observer=session_context.harness_session_id_observer,
        )
    except Exception:
        local_stack.close()
        raise


async def _invoke_runner(
    handoff: PreparedExecutionHandoff,
    *,
    spawn: Spawn,
    runtime: OperationRuntime,
    runtime_root: Path,
    project_paths: ProjectConfigPaths,
    event_observer: Callable[[StreamEvent], None] | None,
    stream_stdout_to_terminal: bool,
    stream_stderr_to_terminal: bool,
    debug: bool,
) -> int:
    """Delegate execution to the streaming runner after preparation succeeds."""

    return await execute_with_streaming(
        spawn,
        request=handoff.resolved_request,
        launch_context=handoff.launch_context,
        project_root=project_paths.project_root,
        runtime_root=runtime_root,
        artifacts=runtime.artifacts,
        harness_session_id_observer=handoff.harness_session_id_observer,
        event_observer=event_observer,
        stream_stdout_to_terminal=stream_stdout_to_terminal,
        stream_stderr_to_terminal=stream_stderr_to_terminal,
        debug=debug,
    )


def _close_execution_handoff(handoff: PreparedExecutionHandoff) -> None:
    """Close session scope owned by a prepared execution handoff."""

    handoff.session_exit_stack.close()


def _prepare_child_claude_overlay(
    *,
    handoff: PreparedExecutionHandoff,
    spawn_id: SpawnId,
    runtime_root: Path,
) -> PreparedClaudeOverlay:
    """Create per-spawn Claude overlay, inject env, and persist metadata."""
    isolated_config_root: Path | None = None
    cleanup_materialization_root: Path | None = None
    try:
        isolated_config_root, original_claude_config_dir = prepare_isolated_claude_config(
            runtime_root=runtime_root,
            spawn_id=str(spawn_id),
        )
        overlay_roots = resolve_claude_overlay_roots(
            isolated_config_root=isolated_config_root,
            original_config_env=original_claude_config_dir,
        )
        cleanup_materialization_root = overlay_roots.materialization_root
        effective_config_root = overlay_roots.effective_config_root
        effective_config_dir = str(effective_config_root)

        child_env = dict(handoff.launch_context.env)
        child_env[MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV] = original_claude_config_dir
        if effective_config_dir:
            child_env["CLAUDE_CONFIG_DIR"] = effective_config_dir
            spawn_store.update_spawn(
                runtime_root,
                spawn_id,
                claude_config_dir=effective_config_dir,
            )
            if handoff.session_context.chat_id:
                update_session_claude_config_dir(
                    runtime_root,
                    handoff.session_context.chat_id,
                    claude_config_dir=effective_config_dir,
                )
        updated_environment = replace(
            handoff.launch_context.binding.environment,
            runner_overlay_env=MappingProxyType(
                {
                    key: value
                    for key, value in child_env.items()
                    if key not in handoff.launch_context.binding.environment.final_env
                    or handoff.launch_context.binding.environment.final_env[key] != value
                }
            ),
            final_env=MappingProxyType(child_env),
        )
        handoff.launch_context = replace(
            handoff.launch_context,
            binding=replace(
                handoff.launch_context.binding,
                environment=updated_environment,
            ),
            env=MappingProxyType(child_env),
        )

        return PreparedClaudeOverlay(
            isolated_config_root=isolated_config_root,
            effective_config_root=effective_config_root,
            materialization_root=cleanup_materialization_root,
        )
    except Exception:
        if isolated_config_root is not None:
            _cleanup_child_claude_overlay(
                isolated_config_root=isolated_config_root,
                spawn_id=spawn_id,
                canonical_root=cleanup_materialization_root,
            )
        raise


def _seed_child_claude_session_access(
    *,
    request: SpawnRequest,
    child_cwd: Path,
    materialization_root: Path | None,
    target_config_root: Path | None,
) -> None:
    """Seed continue/fork transcript into child Claude config root."""

    session_access = resolve_claude_session_access_source(
        request.session,
        child_cwd=child_cwd,
        materialization_root=materialization_root,
        target_config_root=target_config_root,
    )
    if not session_access.should_seed:
        return
    ensure_claude_session_accessible(
        source_session_id=session_access.source_session_id or "",
        source_cwd=session_access.source_cwd,
        child_cwd=child_cwd,
        source_config_root=session_access.source_config_root,
        target_config_root=session_access.target_config_root,
    )


def _cleanup_child_claude_overlay(
    *,
    isolated_config_root: Path | None,
    spawn_id: SpawnId,
    canonical_root: Path | None = None,
) -> Path | None:
    """Materialize transcripts, then remove the child Claude overlay."""

    resolved_canonical_root = (
        canonical_root
        if canonical_root is not None
        else resolve_overlay_materialization_canonical_root()
    )
    cleanup_result = cleanup_claude_overlay(
        isolated_config_root,
        canonical_root=resolved_canonical_root,
    )
    if (
        isolated_config_root is not None
        and cleanup_result.materialized
        and not cleanup_result.removed
    ):
        logger.warning(
            "Child Claude overlay cleanup materialized transcripts but could not remove overlay",
            spawn_id=str(spawn_id),
            overlay_root=str(isolated_config_root),
        )
    if cleanup_result.removed and cleanup_result.materialized:
        return cleanup_result.materialization_root
    return None


async def launch_prepared_spawn(
    *,
    spawn: Spawn,
    request: SpawnRequest,
    runtime_request: LaunchRuntime,
    runtime: OperationRuntime,
    runtime_root: Path,
    project_paths: ProjectConfigPaths,
    spawn_record: SpawnRecord | None = None,
    execution_cwd: str,
    work_id: str | None = None,
    autocompact: int | None = None,
    stream_stdout_to_terminal: bool = False,
    stream_stderr_to_terminal: bool = False,
    event_observer: Callable[[StreamEvent], None] | None = None,
    harness_session_id_observer: Callable[[str], None] | None = None,
    debug: bool = False,
    ctx: RuntimeContext | None = None,
) -> int:
    """Shared post-row, pre-run launch handoff for foreground/background spawns."""

    handoff: PreparedExecutionHandoff
    prelaunch_state = HarnessPrelaunchState()
    try:
        handoff = await _prepare_execution_handoff(
            spawn=spawn,
            request=request,
            runtime_request=runtime_request,
            runtime=runtime,
            runtime_root=runtime_root,
            project_paths=project_paths,
            spawn_record=spawn_record,
            execution_cwd=execution_cwd,
            work_id=work_id,
            autocompact=autocompact,
            ctx=ctx,
        )
    except Exception as exc:
        await finalize_launch_failure(
            runtime_root,
            project_paths.project_root,
            spawn.spawn_id,
            str(exc),
        )
        logger.exception("Pre-launch setup failed.", spawn_id=str(spawn.spawn_id))
        return 1

    try:
        try:
            if harness_session_id_observer is not None:
                handoff_observer = handoff.harness_session_id_observer

                def _combined_harness_session_id_observer(session_id: str) -> None:
                    handoff_observer(session_id)
                    harness_session_id_observer(session_id)

                handoff.harness_session_id_observer = _combined_harness_session_id_observer

            prepare_prelaunch = getattr(handoff.launch_context.harness, "prepare_prelaunch", None)
            child_env: dict[str, str] | None = None
            if callable(prepare_prelaunch):
                child_env = dict(handoff.launch_context.env)

                def _record_effective_config_dir(config_dir: str) -> None:
                    spawn_store.update_spawn(
                        runtime_root,
                        spawn.spawn_id,
                        claude_config_dir=config_dir,
                    )
                    if handoff.session_context.chat_id:
                        update_session_claude_config_dir(
                            runtime_root,
                            handoff.session_context.chat_id,
                            claude_config_dir=config_dir,
                        )

                maybe_prelaunch_state = prepare_prelaunch(
                    runtime_root=runtime_root,
                    spawn_id=spawn.spawn_id,
                    session=handoff.resolved_request.session,
                    child_cwd=handoff.launch_context.child_cwd,
                    child_env=child_env,
                    resolved_harness_session_id="",
                    record_effective_config_dir=_record_effective_config_dir,
                )
                if isinstance(maybe_prelaunch_state, HarnessPrelaunchState):
                    prelaunch_state = maybe_prelaunch_state
            if child_env is not None and prelaunch_state.env_overrides:
                child_env.update(prelaunch_state.env_overrides)
                updated_environment = replace(
                    handoff.launch_context.binding.environment,
                    runner_overlay_env=MappingProxyType(
                        {
                            key: value
                            for key, value in child_env.items()
                            if key not in handoff.launch_context.binding.environment.final_env
                            or handoff.launch_context.binding.environment.final_env[key] != value
                        }
                    ),
                    final_env=MappingProxyType(child_env),
                )
                handoff.launch_context = replace(
                    handoff.launch_context,
                    binding=replace(
                        handoff.launch_context.binding,
                        environment=updated_environment,
                    ),
                    env=MappingProxyType(child_env),
                )
        except Exception as exc:
            await finalize_launch_failure(
                runtime_root,
                project_paths.project_root,
                spawn.spawn_id,
                str(exc),
            )
            logger.exception("Child harness pre-run setup failed.", spawn_id=str(spawn.spawn_id))
            return 1

        return await _invoke_runner(
            handoff,
            spawn=spawn,
            runtime=runtime,
            runtime_root=runtime_root,
            project_paths=project_paths,
            event_observer=event_observer,
            stream_stdout_to_terminal=stream_stdout_to_terminal,
            stream_stderr_to_terminal=stream_stderr_to_terminal,
            debug=debug,
        )
    finally:
        try:
            cleanup_prelaunch = getattr(handoff.launch_context.harness, "cleanup_prelaunch", None)
            if callable(cleanup_prelaunch):
                cleanup_prelaunch(
                    runtime_root=runtime_root,
                    spawn_id=spawn.spawn_id,
                    chat_id=handoff.session_context.chat_id,
                    state=prelaunch_state,
                )
        except Exception:
            logger.warning(
                "Failed to clean up adapter prelaunch state for child spawn",
                spawn_id=str(spawn.spawn_id),
                exc_info=True,
            )
        try:
            _close_execution_handoff(handoff)
        except Exception:
            logger.warning(
                "Post-run session teardown failed.",
                spawn_id=str(spawn.spawn_id),
                exc_info=True,
            )


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
        autocompact=request.autocompact,
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
    launch_command = _build_background_worker_command(
        spawn_id=spawn_id_text,
        project_paths=project_paths,
    )
    _record_launch_boundary_observation(
        context.runtime_root,
        spawn_id_text,
        event=EVENT_PARENT_LAUNCH_ATTEMPT,
        stage="parent_prepare",
        parent_pid=os.getpid(),
        command=launch_command,
        cwd=str(project_paths.execution_cwd),
    )
    try:
        persisted_request = request.model_copy(update={"work_id_hint": context.work_id})
        launch_runtime = LaunchRuntime(
            argv_intent=LaunchArgvIntent.SPEC_ONLY,
            debug=payload.debug,
            runtime_override_snapshot=RuntimeOverrides.from_env().model_dump(
                mode="json",
                exclude_none=True,
            ),
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
        _record_launch_boundary_observation(
            context.runtime_root,
            spawn_id_text,
            event=EVENT_PARENT_LAUNCH_FAILED,
            stage="persist_worker_request",
            parent_pid=os.getpid(),
            command=launch_command,
            cwd=str(project_paths.execution_cwd),
            error=str(exc),
            exception=exc,
        )
        finalize_launch_failure_sync(
            context.runtime_root,
            project_paths.project_root,
            context.spawn.spawn_id,
            str(exc),
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
        _record_launch_boundary_observation(
            context.runtime_root,
            spawn_id_text,
            event=EVENT_PARENT_LAUNCH_FAILED,
            stage="popen",
            parent_pid=os.getpid(),
            command=launch_command,
            cwd=str(project_paths.execution_cwd),
            error=str(exc),
            exception=exc,
        )
        finalize_launch_failure_sync(
            context.runtime_root,
            project_paths.project_root,
            context.spawn.spawn_id,
            str(exc),
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

    _record_launch_boundary_observation(
        context.runtime_root,
        spawn_id_text,
        event=EVENT_PARENT_LAUNCH_SPAWNED,
        stage="popen",
        parent_pid=os.getpid(),
        launcher_pid=process.pid,
        command=launch_command,
        cwd=str(project_paths.execution_cwd),
    )
    build_spawn_lifecycle_service_from_roots(
        project_paths.project_root,
        context.runtime_root,
    ).mark_running(
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
                    runtime_override_snapshot=RuntimeOverrides.from_env().model_dump(
                        mode="json",
                        exclude_none=True,
                    ),
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
        finalize_launch_failure_sync(
            context.runtime_root,
            project_paths.project_root,
            spawn.spawn_id,
            str(exc),
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
    "depth_exceeded_output",
    "depth_limits",
    "execute_spawn_background",
    "execute_spawn_blocking",
    "launch_prepared_spawn",
]
