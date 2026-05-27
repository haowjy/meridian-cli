"""Spawn initialization helpers: init_spawn, write_params, depth utilities."""

import json
import time
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.bootstrap.services import build_spawn_lifecycle_service_from_roots
from meridian.lib.config.project_paths import ProjectConfigPaths, resolve_project_config_paths
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.depth import current_meridian_depth, max_depth_reached
from meridian.lib.core.domain import Spawn, SpawnStatus
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.sink import OutputSink
from meridian.lib.core.types import ModelId, SpawnId
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.ops.work_attachment import ensure_explicit_work_item
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import (
    resolve_project_paths,
    resolve_spawn_log_dir,
    resolve_work_scratch_dir_for_project,
)
from meridian.lib.state.spawn.model import LaunchMode

from ..runtime import OperationRuntime, resolve_chat_id, resolve_runtime_root, runtime_context
from .models import SpawnActionOutput, SpawnCreateInput

logger = structlog.get_logger(__name__)


class LaunchUserInputError(ValueError):
    """Expected launch-input failure that should not emit traceback logging."""


class _SpawnContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn: Spawn
    runtime_root: Path
    current_depth: int
    work_id: str | None = None


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


def depth_limits(max_depth: int, *, ctx: RuntimeContext | None = None) -> tuple[int, int]:
    current_depth = runtime_context(ctx).depth
    max_depth_reached(current_depth, max_depth)
    return current_depth, max_depth


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

    # Omit project_root/runtime_root/chat_id (leave at defaults) — those are
    # already correct in the inherited os.environ.  increment_depth=False because
    # the background worker is a peer, not a depth-child.
    child_env = ResolvedContext(
        depth=parent_depth,
        work_id=normalized_work_id,
        work_dir=work_dir,
    ).child_env_overrides(increment_depth=False)
    if autocompact is not None:
        child_env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(autocompact)
    return child_env


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
    return None


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
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
    control_root: str | None = None,
    task_cwd: str | None = None,
    execution_cwd: str | None = None,
    ctx: RuntimeContext | None = None,
) -> _SpawnContext:
    from typing import cast

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
    spawn_session_metadata = PrimarySessionMetadata(
        harness=request.harness or "",
        model=request.model or "",
        agent=request.agent or "",
        agent_path=request.agent_metadata.get("session_agent_path") or "",
        skills=request.skills,
        skill_paths=request.skill_paths,
    )
    spawn_id = service.start(
        chat_id=resolve_chat_id(ctx=resolved_context, fallback="c0"),
        parent_id=str(resolved_context.spawn_id) if resolved_context.spawn_id else None,
        session_metadata=spawn_session_metadata,
        kind="child",
        prompt=request.prompt,
        desc=resolved_desc,
        work_id=resolved_work_id,
        goal=request.goal,
        # I-10: do NOT pre-populate harness_session_id on fork starts.
        # materialize_fork() writes it via update_spawn after the row exists.
        harness_session_id=(
            None if request.session.continue_fork else request.session.requested_harness_session_id
        ),
        control_root=control_root,
        task_cwd=task_cwd,
        execution_cwd=execution_cwd,
        launch_mode=launch_mode,
        runner_pid=runner_pid,
        launch_policy_snapshot=launch_policy_snapshot or request.launch_policy_snapshot,
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
        "goal": request.goal,
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


__all__ = [
    "LaunchUserInputError",
    "_SpawnContext",
    "_emit_subrun_event",
    "_init_spawn",
    "_spawn_background_worker_env",
    "_spawn_child_env",
    "_write_params_json",
    "depth_exceeded_output",
    "depth_limits",
]
