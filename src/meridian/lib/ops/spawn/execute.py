"""Spawn execution entry points: execute_spawn_blocking, execute_spawn_background."""

import asyncio
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from meridian.lib.bootstrap.services import build_spawn_lifecycle_service_from_roots
from meridian.lib.config.project_paths import resolve_project_config_paths
from meridian.lib.core.context import RuntimeContext
from meridian.lib.launch.context import PreparedLaunchSurface
from meridian.lib.launch.cwd import resolve_task_cwd
from meridian.lib.launch.request import LaunchArgvIntent, SpawnRequest
from meridian.lib.ops.work_attachment import ensure_explicit_work_item
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_boundary import (
    EVENT_PARENT_LAUNCH_ATTEMPT,
    EVENT_PARENT_LAUNCH_FAILED,
    EVENT_PARENT_LAUNCH_SPAWNED,
)
from meridian.lib.state.paths import resolve_project_paths, resolve_spawn_log_dir
from meridian.lib.state.spawn.model import (
    BACKGROUND_LAUNCH_MODE,
    FOREGROUND_LAUNCH_MODE,
    LaunchMode,
)

from ..runtime import OperationRuntime, runtime_context
from .execute_bg import (
    BACKGROUND_STDERR_FILENAME,
    BACKGROUND_STDOUT_FILENAME,
    BackgroundWorkerLaunchRequest,
    _background_worker_main,
    _build_background_worker_command,
    _cleanup_background_runtime_artifacts,
    _persist_bg_worker_request,
    _record_launch_boundary_observation,
)
from .execute_init import (
    _announce_reserved_spawn,
    _emit_subrun_event,
    _init_spawn,
    _spawn_background_worker_env,
    _SpawnContext,
    _write_params_json,
    build_spawn_mars_runtime,
    depth_exceeded_output,
    depth_limits,
    resolve_spawn_work_id,
)
from .execute_runner import launch_prepared_spawn
from .failure_policy import finalize_launch_failure_sync
from .models import SpawnActionOutput, SpawnCreateInput
from .pre_init import EXPECTED_PRE_INIT_EXCEPTIONS, PreInitFailure, run_pre_init_boundary
from .query import read_report, read_spawn_row

logger = structlog.get_logger(__name__)
_BACKGROUND_SUBMIT_MESSAGE = "Background spawn submitted."


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


def _task_cwd_from_execution(*, control_root: Path, execution_cwd: str) -> str | None:
    resolved_execution_cwd = Path(execution_cwd).expanduser().resolve()
    if resolved_execution_cwd == control_root.resolve():
        return None
    return resolved_execution_cwd.as_posix()


@dataclass(frozen=True)
class SpawnExecutionContract:
    control_root: Path
    execution_cwd: Path
    task_cwd: str | None
    work_id: str | None


@dataclass(frozen=True)
class SpawnExecutionPreparation:
    resolved_context: RuntimeContext
    project_paths: Any
    execution_contract: SpawnExecutionContract


def _resolve_execution_contract(
    *,
    request: SpawnRequest,
    project_paths: Any,
    payload: SpawnCreateInput,
    ctx: RuntimeContext,
) -> SpawnExecutionContract:
    request_control_root = (request.authority_root or "").strip()
    control_root = (
        Path(request_control_root).expanduser().resolve()
        if request_control_root
        else project_paths.project_root.resolve()
    )
    request_task_cwd = (request.task_cwd or "").strip()
    if request_task_cwd:
        execution_cwd = Path(request_task_cwd).expanduser().resolve()
    else:
        explicit_work_id = payload.work.strip() or None
        execution_cwd = resolve_task_cwd(
            project_paths.project_root,
            project_state_dir=resolve_project_paths(project_paths.project_root).root_dir,
            explicit_task_dir=payload.task_dir,
            explicit_work_id=explicit_work_id,
            ambient_work_id=None if explicit_work_id else ctx.work_id,
            caller_cwd=payload.caller_cwd,
        ).task_cwd

    work_id = resolve_spawn_work_id(payload, request, ctx=ctx)
    execution_cwd_str = execution_cwd.as_posix()
    return SpawnExecutionContract(
        control_root=control_root,
        execution_cwd=execution_cwd,
        task_cwd=_task_cwd_from_execution(
            control_root=control_root,
            execution_cwd=execution_cwd_str,
        ),
        work_id=work_id,
    )


def _prepare_spawn_execution(
    *,
    payload: SpawnCreateInput,
    request: SpawnRequest,
    runtime: OperationRuntime,
    ctx: RuntimeContext | None,
) -> SpawnExecutionPreparation | SpawnActionOutput:
    def _operation() -> SpawnExecutionPreparation:
        try:
            resolved_context = runtime_context(ctx)
            project_paths = resolve_project_config_paths(
                project_root=runtime.project_root,
                execution_cwd=runtime.authority.execution_cwd,
            )
            execution_contract = _resolve_execution_contract(
                request=request,
                project_paths=project_paths,
                payload=payload,
                ctx=resolved_context,
            )
            return SpawnExecutionPreparation(
                resolved_context=resolved_context,
                project_paths=project_paths,
                execution_contract=execution_contract,
            )
        except EXPECTED_PRE_INIT_EXCEPTIONS as exc:
            raise PreInitFailure(str(exc)) from exc

    return run_pre_init_boundary(payload=payload, request=request, operation=_operation)


def _persist_execution_contract(
    context: _SpawnContext,
    execution_contract: SpawnExecutionContract,
) -> None:
    execution_cwd_str = execution_contract.execution_cwd.as_posix()
    spawn_store.update_spawn(
        context.runtime_root,
        context.spawn.spawn_id,
        control_root=execution_contract.control_root.as_posix(),
        task_cwd=_task_cwd_from_execution(
            control_root=execution_contract.control_root,
            execution_cwd=execution_cwd_str,
        ),
        execution_cwd=execution_cwd_str,
    )


def _reserve_then_prepare(
    *,
    payload: SpawnCreateInput,
    request: SpawnRequest,
    runtime: OperationRuntime,
    ctx: RuntimeContext | None,
    launch_mode: LaunchMode,
    runner_pid: int | None = None,
) -> tuple[_SpawnContext, SpawnExecutionPreparation] | SpawnActionOutput:
    resolved_context = runtime_context(ctx)
    context = _init_spawn(
        payload=payload,
        request=request,
        runtime=runtime,
        desc=payload.desc,
        work_id=resolve_spawn_work_id(payload, request, ctx=resolved_context),
        status="queued",
        launch_mode=launch_mode,
        runner_pid=runner_pid,
        launch_policy_snapshot=request.launch_policy_snapshot,
        ctx=resolved_context,
        announce=False,
    )
    preparation = _prepare_spawn_execution(
        payload=payload,
        request=request,
        runtime=runtime,
        ctx=ctx,
    )
    if isinstance(preparation, SpawnActionOutput):
        spawn_store.remove_spawn_events(context.runtime_root, context.spawn.spawn_id)
        return preparation

    project_paths = preparation.project_paths
    final_work_id = context.work_id
    if (payload.work or "").strip():
        from typing import cast

        project_local_root = resolve_project_paths(project_paths.project_root).root_dir
        final_work_id = cast("str", final_work_id)
        final_work_id = ensure_explicit_work_item(project_local_root, final_work_id)
        if final_work_id != context.work_id:
            spawn_store.update_spawn(
                context.runtime_root,
                context.spawn.spawn_id,
                work_id=final_work_id,
            )
    context = context.model_copy(update={"work_id": final_work_id})

    _persist_execution_contract(context, preparation.execution_contract)
    _announce_reserved_spawn(
        context=context,
        request=request,
        runtime=runtime,
        project_root=project_paths.project_root,
        ctx=resolved_context,
    )
    return context, preparation


def execute_spawn_background(
    *,
    payload: SpawnCreateInput,
    request: SpawnRequest,
    runtime: OperationRuntime,
    ctx: RuntimeContext | None = None,
) -> SpawnActionOutput:
    reserved = _reserve_then_prepare(
        payload=payload,
        request=request,
        runtime=runtime,
        ctx=ctx,
        launch_mode=BACKGROUND_LAUNCH_MODE,
    )
    if isinstance(reserved, SpawnActionOutput):
        return reserved
    context, preparation = reserved
    project_paths = preparation.project_paths
    execution_contract = preparation.execution_contract

    initial_execution_cwd = execution_contract.execution_cwd.as_posix()
    if payload.stream:
        logger.warning("--stream requires --foreground; output goes to spawn log files.")
    spawn_id_text = str(context.spawn.spawn_id)
    execution_cwd_str = initial_execution_cwd
    parent_launch_cwd_str = execution_contract.control_root.as_posix()
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
        cwd=parent_launch_cwd_str,
    )
    try:
        persisted_request = request.model_copy(update={"work_id_hint": context.work_id})
        launch_runtime = build_spawn_mars_runtime(
            runtime=runtime,
            runtime_root=context.runtime_root,
            control_root=execution_contract.control_root,
            execution_cwd=execution_cwd_str,
            argv_intent=LaunchArgvIntent.SPEC_ONLY,
            debug=payload.debug,
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
            cwd=parent_launch_cwd_str,
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
            authority_root=execution_contract.control_root.as_posix(),
            task_cwd=request.task_cwd or execution_cwd_str,
            reference_anchor=request.reference_anchor or execution_cwd_str,
            task_cwd_source=request.task_cwd_source,
            task_cwd_work_item=request.task_cwd_work_item,
        )

    stdout_path = log_dir / BACKGROUND_STDOUT_FILENAME
    stderr_path = log_dir / BACKGROUND_STDERR_FILENAME

    launch_env = dict(os.environ)
    launch_env.update(
        _spawn_background_worker_env(
            project_root=execution_contract.control_root,
            work_id=context.work_id,
            autocompact=request.execution_policy.autocompact,
        )
    )
    try:
        with (
            stdout_path.open("ab") as stdout_handle,
            stderr_path.open("ab") as stderr_handle,
        ):
            process = subprocess.Popen(
                launch_command,
                cwd=parent_launch_cwd_str,
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
            cwd=parent_launch_cwd_str,
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
            authority_root=execution_contract.control_root.as_posix(),
            task_cwd=request.task_cwd or execution_cwd_str,
            reference_anchor=request.reference_anchor or execution_cwd_str,
            task_cwd_source=request.task_cwd_source,
            task_cwd_work_item=request.task_cwd_work_item,
        )

    _record_launch_boundary_observation(
        context.runtime_root,
        spawn_id_text,
        event=EVENT_PARENT_LAUNCH_SPAWNED,
        stage="popen",
        parent_pid=os.getpid(),
        launcher_pid=process.pid,
        command=launch_command,
        cwd=parent_launch_cwd_str,
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
        authority_root=execution_contract.control_root.as_posix(),
        task_cwd=request.task_cwd or execution_cwd_str,
        reference_anchor=request.reference_anchor or execution_cwd_str,
        task_cwd_source=request.task_cwd_source,
        task_cwd_work_item=request.task_cwd_work_item,
    )


def execute_spawn_blocking(
    *,
    payload: SpawnCreateInput,
    request: SpawnRequest,
    runtime: OperationRuntime,
    ctx: RuntimeContext | None = None,
    prepared: PreparedLaunchSurface | None = None,
    on_spawn_id: Callable[[str], None] | None = None,
) -> SpawnActionOutput:
    reserved = _reserve_then_prepare(
        payload=payload,
        request=request,
        runtime=runtime,
        ctx=ctx,
        launch_mode=FOREGROUND_LAUNCH_MODE,
        runner_pid=os.getpid(),
    )
    if isinstance(reserved, SpawnActionOutput):
        return reserved
    context, preparation = reserved
    resolved_context = preparation.resolved_context
    project_paths = preparation.project_paths
    execution_contract = preparation.execution_contract

    initial_execution_cwd = execution_contract.execution_cwd.as_posix()
    spawn = context.spawn
    spawn_id_text = str(spawn.spawn_id)
    if on_spawn_id is not None:
        on_spawn_id(spawn_id_text)

    warning = request.warning
    context_from_resolved = request.context_from

    try:
        execution_cwd_str = initial_execution_cwd
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
                spawn_id=spawn_id_text,
                exc_info=True,
            )
        started = time.monotonic()
        stream_stdout_to_terminal = payload.stream
        event_observer = None
        # Spawn execution stays silent unless --stream is explicitly enabled.

        exit_code = asyncio.run(
            launch_prepared_spawn(
                spawn=spawn,
                request=request,
                runtime_request=build_spawn_mars_runtime(
                    runtime=runtime,
                    runtime_root=context.runtime_root,
                    control_root=execution_contract.control_root,
                    execution_cwd=execution_cwd_str,
                    argv_intent=LaunchArgvIntent.SPEC_ONLY,
                    debug=payload.debug,
                ),
                runtime=runtime,
                runtime_root=context.runtime_root,
                project_paths=project_paths,
                execution_cwd=execution_cwd_str,
                work_id=context.work_id,
                stream_stdout_to_terminal=stream_stdout_to_terminal,
                stream_stderr_to_terminal=payload.stream,
                event_observer=event_observer,
                debug=payload.debug,
                ctx=resolved_context,
                prepared=prepared,
            )
        )
    except Exception as exc:
        finalize_launch_failure_sync(
            context.runtime_root,
            project_paths.project_root,
            spawn.spawn_id,
            str(exc),
        )
        logger.exception("Foreground spawn crashed.", spawn_id=spawn_id_text)
        return SpawnActionOutput(
            command="spawn.create",
            status="failed",
            spawn_id=spawn_id_text,
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
            authority_root=execution_contract.control_root.as_posix(),
            task_cwd=request.task_cwd or initial_execution_cwd,
            reference_anchor=request.reference_anchor or initial_execution_cwd,
            task_cwd_source=request.task_cwd_source,
            task_cwd_work_item=request.task_cwd_work_item,
        )

    duration = time.monotonic() - started
    row = read_spawn_row(project_paths.project_root, str(spawn.spawn_id))
    _, report_body = read_report(
        project_paths.project_root,
        str(spawn.spawn_id),
        include_body=True,
        runtime_root=context.runtime_root,
    )
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
            "id": spawn_id_text,
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
        spawn_id=spawn_id_text,
        message="Spawn completed.",
        model=request.model or "",
        harness_id=request.harness or "",
        warning=warning,
        agent=request.agent,
        reference_files=request.reference_files,
        template_vars=request.template_vars,
        context_from_resolved=context_from_resolved,
        report=report_body,
        exit_code=exit_code,
        duration_secs=done_secs,
        authority_root=execution_contract.control_root.as_posix(),
        task_cwd=request.task_cwd or initial_execution_cwd,
        reference_anchor=request.reference_anchor or initial_execution_cwd,
        task_cwd_source=request.task_cwd_source,
        task_cwd_work_item=request.task_cwd_work_item,
    )


# Backward-compat __main__ entry: forwards to the background worker main
# (which now lives in execute_bg.py). Queued spawns launched with the old
# `python -m meridian.lib.ops.spawn.execute` command still work.
if __name__ == "__main__":
    raise SystemExit(_background_worker_main())


__all__ = [
    "depth_exceeded_output",
    "depth_limits",
    "execute_spawn_background",
    "execute_spawn_blocking",
    "launch_prepared_spawn",
]
