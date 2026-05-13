"""Spawn execution runner: prepare handoff, invoke runner, launch_prepared_spawn."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

import structlog

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import ForkMaterializationMode, HarnessPrelaunchState, StreamEvent
from meridian.lib.launch.artifact_io import write_projection_artifacts
from meridian.lib.launch.context import LaunchContext, build_launch_context
from meridian.lib.launch.fork import materialize_fork
from meridian.lib.launch.request import LaunchArgvIntent, LaunchRuntime, SpawnRequest
from meridian.lib.launch.streaming_runner import execute_with_streaming
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.session_store import update_session_claude_config_dir
from meridian.lib.state.spawn.model import SpawnRecord

from ..runtime import OperationRuntime, runtime_context
from .execute_init import LaunchUserInputError, _spawn_child_env
from .execute_session import (
    _resolve_session_continuation,
    _session_execution_context,
    _SessionExecutionContext,
)
from .failure_policy import finalize_launch_failure

logger = structlog.get_logger(__name__)


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


def _log_launch_failure_without_traceback(
    *,
    message: str,
    spawn_id: SpawnId,
    exc: Exception,
) -> None:
    if isinstance(exc, LaunchUserInputError):
        logger.warning(message, spawn_id=str(spawn_id), error=str(exc))
        return
    logger.exception(message, spawn_id=str(spawn_id))


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
            resolved_agent_name = request.agent if request.agent is not None else spawn_record.agent

        session_metadata = PrimarySessionMetadata(
            harness=request.harness or "",
            model=request.model or "",
            agent=(
                (spawn_record.agent if spawn_record else "")
                or request.agent_metadata.get("session_agent", "")
            ),
            agent_path=(
                (spawn_record.agent_path if spawn_record else "")
                or request.agent_metadata.get("session_agent_path", "")
            ),
            skills=request.skills or (spawn_record.skills if spawn_record else ()),
            skill_paths=(spawn_record.skill_paths if spawn_record else request.skill_paths),
        )
        session_context = local_stack.enter_context(
            _session_execution_context(
                runtime_root=runtime_root,
                metadata=session_metadata,
                request=resolved_session,
                harness_session_id=(
                    resolved_session.requested_harness_session_id
                    or (spawn_record.harness_session_id if spawn_record else "")
                    or ""
                ),
                run_agent_name=resolved_agent_name,
                inherited_work_id=work_id,
                control_root=runtime_request.resolved_control_root,
                task_cwd=(
                    execution_cwd
                    if Path(execution_cwd).resolve()
                    != Path(runtime_request.resolved_control_root).resolve()
                    else None
                ),
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
            try:
                forked_session_id = materialize_fork(
                    adapter=harness_adapter,
                    source_session_id=resolved_session.requested_harness_session_id,
                    runtime_root=runtime_root,
                    spawn_id=spawn.spawn_id,
                )
            except ValueError as exc:
                raise LaunchUserInputError(str(exc)) from exc
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
            autocompact=request.execution_policy.autocompact,
            ctx=resolved_context,
        )
        runtime_work_id = session_context.work_id or work_id
        final_request = resolved_request.model_copy(update={"work_id_hint": runtime_work_id})
        launch_runtime = runtime_request.model_copy(
            update={
                "argv_intent": LaunchArgvIntent.SPEC_ONLY,
                "runtime_root": runtime_root.as_posix(),
                "config_root": project_paths.project_root.as_posix(),
                "control_root": runtime_request.resolved_control_root,
                "requested_task_cwd": execution_cwd,
                # Legacy aliases.
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
            ctx=ctx,
        )
    except Exception as exc:
        await finalize_launch_failure(
            runtime_root,
            project_paths.project_root,
            spawn.spawn_id,
            str(exc),
        )
        _log_launch_failure_without_traceback(
            message="Pre-launch setup failed.",
            spawn_id=spawn.spawn_id,
            exc=exc,
        )
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
                child_env = dict(handoff.launch_context.binding.environment.final_env)

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
                    child_cwd=handoff.launch_context.control_root,
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
                )
        except Exception as exc:
            await finalize_launch_failure(
                runtime_root,
                project_paths.project_root,
                spawn.spawn_id,
                str(exc),
            )
            _log_launch_failure_without_traceback(
                message="Child harness pre-run setup failed.",
                spawn_id=spawn.spawn_id,
                exc=exc,
            )
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


__all__ = [
    "PreparedExecutionHandoff",
    "_close_execution_handoff",
    "_invoke_runner",
    "_prepare_execution_handoff",
    "launch_prepared_spawn",
]
