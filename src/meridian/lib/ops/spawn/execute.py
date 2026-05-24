"""Spawn execution entry points: execute_spawn_blocking, execute_spawn_background."""

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from meridian.lib.bootstrap.services import build_spawn_lifecycle_service_from_roots
from meridian.lib.config.project_paths import resolve_project_config_paths
from meridian.lib.core.context import RuntimeContext
from meridian.lib.launch.cwd import resolve_task_cwd
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_boundary import (
    EVENT_PARENT_LAUNCH_ATTEMPT,
    EVENT_PARENT_LAUNCH_FAILED,
    EVENT_PARENT_LAUNCH_SPAWNED,
)
from meridian.lib.state.paths import resolve_project_paths, resolve_spawn_log_dir
from meridian.lib.state.spawn.model import BACKGROUND_LAUNCH_MODE, FOREGROUND_LAUNCH_MODE

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
    _emit_subrun_event,
    _init_spawn,
    _spawn_background_worker_env,
    _write_params_json,
    build_spawn_execution_launch_runtime,
    depth_exceeded_output,
    depth_limits,
)
from .execute_runner import launch_prepared_spawn
from .failure_policy import finalize_launch_failure_sync
from .models import SpawnActionOutput, SpawnCreateInput
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


def _resolve_execution_contract(
    *,
    request: SpawnRequest,
    project_paths: Any,
    payload: SpawnCreateInput,
    ambient_work_id: str | None,
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
            explicit_work_id=explicit_work_id,
            ambient_work_id=None if explicit_work_id else ambient_work_id,
            force_worktree=payload.worktree is True,
            force_no_worktree=payload.worktree is False,
        ).task_cwd

    work_id = (
        (request.task_cwd_work_item or "").strip()
        or (request.work_id_hint or "").strip()
        or payload.work.strip()
        or (ambient_work_id or "").strip()
        or None
    )
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


def execute_spawn_background(
    *,
    payload: SpawnCreateInput,
    request: SpawnRequest,
    runtime: OperationRuntime,
    ctx: RuntimeContext | None = None,
) -> SpawnActionOutput:
    resolved_context = runtime_context(ctx)
    project_paths = resolve_project_config_paths(
        project_root=runtime.project_root,
        execution_cwd=runtime.authority.execution_cwd,
    )
    execution_contract = _resolve_execution_contract(
        request=request,
        project_paths=project_paths,
        payload=payload,
        ambient_work_id=resolved_context.work_id,
    )
    initial_execution_cwd = execution_contract.execution_cwd.as_posix()
    initial_task_cwd = execution_contract.task_cwd
    if payload.stream:
        logger.warning("--stream requires --foreground; output goes to spawn log files.")
    context = _init_spawn(
        payload=payload,
        request=request,
        runtime=runtime,
        desc=payload.desc,
        work_id=execution_contract.work_id,
        status="queued",
        launch_mode=BACKGROUND_LAUNCH_MODE,
        control_root=execution_contract.control_root.as_posix(),
        task_cwd=initial_task_cwd,
        execution_cwd=initial_execution_cwd,
        launch_policy_snapshot=request.launch_policy_snapshot,
        ctx=resolved_context,
    )
    spawn_id_text = str(context.spawn.spawn_id)
    execution_cwd_str = initial_execution_cwd
    parent_launch_cwd_str = execution_contract.control_root.as_posix()
    task_cwd_str = _task_cwd_from_execution(
        control_root=execution_contract.control_root,
        execution_cwd=execution_cwd_str,
    )
    # Record resolved execution_cwd immediately so it's correct even if
    # the background worker dies before runner.py's authoritative update.
    spawn_store.update_spawn(
        context.runtime_root,
        context.spawn.spawn_id,
        control_root=execution_contract.control_root.as_posix(),
        task_cwd=task_cwd_str,
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
        cwd=parent_launch_cwd_str,
    )
    try:
        persisted_request = request.model_copy(update={"work_id_hint": context.work_id})
        launch_runtime = build_spawn_execution_launch_runtime(
            runtime=runtime,
            runtime_root=context.runtime_root,
            control_root=execution_contract.control_root,
            execution_cwd=execution_cwd_str,
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
) -> SpawnActionOutput:
    resolved_context = runtime_context(ctx)
    project_paths = resolve_project_config_paths(
        project_root=runtime.project_root,
        execution_cwd=runtime.authority.execution_cwd,
    )
    execution_contract = _resolve_execution_contract(
        request=request,
        project_paths=project_paths,
        payload=payload,
        ambient_work_id=resolved_context.work_id,
    )
    initial_execution_cwd = execution_contract.execution_cwd.as_posix()
    initial_task_cwd = execution_contract.task_cwd
    context = _init_spawn(
        payload=payload,
        request=request,
        runtime=runtime,
        desc=payload.desc,
        work_id=execution_contract.work_id,
        status="queued",
        launch_mode=FOREGROUND_LAUNCH_MODE,
        runner_pid=os.getpid(),
        control_root=execution_contract.control_root.as_posix(),
        task_cwd=initial_task_cwd,
        execution_cwd=initial_execution_cwd,
        launch_policy_snapshot=request.launch_policy_snapshot,
        ctx=resolved_context,
    )
    spawn = context.spawn

    warning = request.warning
    context_from_resolved = request.context_from

    try:
        execution_cwd_str = initial_execution_cwd
        # Reflect selected task_cwd before launch_prepared_spawn.
        spawn_store.update_spawn(
            context.runtime_root,
            spawn.spawn_id,
            control_root=execution_contract.control_root.as_posix(),
            task_cwd=_task_cwd_from_execution(
                control_root=execution_contract.control_root,
                execution_cwd=execution_cwd_str,
            ),
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
        started = time.monotonic()
        stream_stdout_to_terminal = payload.stream
        event_observer = None
        # Spawn execution stays silent unless --stream is explicitly enabled.

        exit_code = asyncio.run(
            launch_prepared_spawn(
                spawn=spawn,
                request=request,
                runtime_request=build_spawn_execution_launch_runtime(
                    runtime=runtime,
                    runtime_root=context.runtime_root,
                    control_root=execution_contract.control_root,
                    execution_cwd=execution_cwd_str,
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
