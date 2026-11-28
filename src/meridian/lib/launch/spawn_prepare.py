"""Prepare a spawn: compose its launch, publish its row, and project its connection."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from meridian.lib.catalog.model_aliases import MarsResultCache
from meridian.lib.core.domain import PrimarySessionMetadata
from meridian.lib.core.spawn_start import SpawnStartMetadata
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.launch.artifact_io import write_projection_artifacts
from meridian.lib.launch.composition_spawn import (
    bind_spawn_launch_context,
    compose_spawn_launch_surface,
)
from meridian.lib.launch.context import (
    LaunchContext,
    RuntimeBindings,
    materialize_launch_context,
)
from meridian.lib.launch.env import apply_pi_bind_time_env, resolve_pi_session_role
from meridian.lib.launch.fork import fork_usage_seed
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.launch.resolve import (
    resolve_pi_child_wave_timeout_seconds,
    resolve_pi_task_ping_interval_seconds,
    resolve_resident_deadline_seconds,
    resolve_resident_poll_seconds,
)
from meridian.lib.spawn.lifecycle import SpawnReservation
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_lock import LaunchSlot
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn.model import LaunchMode
from meridian.plugin_api.types import SpawnKind

if TYPE_CHECKING:
    from meridian.lib.harness.registry import HarnessRegistry
    from meridian.lib.observability.debug_tracer import DebugTracer
    from meridian.lib.spawn.service import SpawnApplicationService


@dataclass(frozen=True)
class PreparedSpawn:
    spawn_id: SpawnId
    launch: LaunchPaths
    launch_context: LaunchContext
    connection_config: ConnectionConfig
    resolved_model: str
    resolved_agent: str | None
    resolved_harness: str
    work_id: str | None


def _config_snapshot_env(config_snapshot: dict[str, object]) -> dict[str, str]:
    env = config_snapshot.get("env")
    if isinstance(env, dict):
        result: dict[str, str] = {}
        for k, v in cast("dict[str, object]", env).items():
            if not k.strip():
                continue
            if not isinstance(v, str):
                continue
            result[k] = v
        return result
    return {}


async def prepare_spawn(
    service: SpawnApplicationService,
    *,
    request: SpawnRequest,
    runtime: LaunchRuntime,
    harness_registry: HarnessRegistry,
    chat_id: str | None = None,
    parent_id: str | None = None,
    kind: SpawnKind = "child",
    desc: str | None = None,
    work_id: str | None = None,
    launch_mode: LaunchMode | None = None,
    runner_pid: int | None = None,
    debug_tracer: DebugTracer | None = None,
    launch_lock: LaunchSlot,
) -> PreparedSpawn:
    """Resolve launch context, create spawn row, and project ConnectionConfig.

    No spawn row is created until launch preparation succeeds. Its metadata
    reflects resolved values, and ID allocation is atomic with row creation.
    ConnectionConfig is projected from LaunchContext.

    Raises on resolution failure, with no spawn row. If writing the launch files
    (``system-prompt.md``) fails after the row is persisted, the row stays
    ``running`` until the reaper finalizes it.
    On success, row exists with resolved metadata.
    """
    mars_cache = MarsResultCache()
    prepared_surface = await asyncio.to_thread(
        compose_spawn_launch_surface,
        request=request,
        runtime=runtime,
        harness_registry=harness_registry,
        dry_run=False,
        runtime_work_id=(work_id or "").strip() or None,
        cache=mars_cache,
    )

    resolved_request = prepared_surface.request
    resolved_model = (resolved_request.model or "").strip()
    resolved_harness = (resolved_request.harness or "").strip()
    resolved_agent = (resolved_request.agent or "").strip() or None

    if not resolved_harness:
        raise ValueError("Harness resolution failed - harness is required")

    effective_work_id = (work_id or "").strip() or None

    reserved_launch = await asyncio.to_thread(
        spawn_store.reserve_launch,
        service.runtime_root,
    )
    final_spawn_id = reserved_launch.spawn_id
    config_env = _config_snapshot_env(runtime.config_snapshot)
    request_env = dict(prepared_surface.request.env)
    resolved_env = {**config_env, **request_env}
    launch_ctx = await asyncio.to_thread(
        bind_spawn_launch_context,
        prepared=prepared_surface,
        bindings=RuntimeBindings(
            launch=reserved_launch,
            runtime_work_id=effective_work_id,
            dry_run=False,
            plan_overrides=resolved_env,
        ),
        runtime=runtime,
        harness_registry=harness_registry,
    )
    launch_ctx = await asyncio.to_thread(materialize_launch_context, launch_ctx)
    resolved_request = launch_ctx.resolved_request
    resolved_model = (resolved_request.model or "").strip()
    resolved_harness = (resolved_request.harness or "").strip()
    resolved_agent = (resolved_request.agent or "").strip() or None
    if not resolved_harness:
        raise ValueError("Harness resolution failed - harness is required")
    effective_work_id = (work_id or launch_ctx.work_id or "").strip() or None
    start_metadata = SpawnStartMetadata(
        forked_from_history_id=resolved_request.session.forked_from_history_id,
        usage_seed=fork_usage_seed(service.runtime_root, resolved_request.session),
        desc=desc,
        work_id=effective_work_id,
        goal=getattr(resolved_request, "goal", None),
        launch_policy_snapshot=resolved_request.launch_policy_snapshot,
    )

    # Persist the already-reserved ID via lifecycle service only
    # after launch context composition succeeds. Failed composition leaves no
    # spawn row and emits no spawn.created hook/telemetry.
    prepare_session_metadata = PrimarySessionMetadata(
        harness=resolved_harness,
        model=resolved_model,
        agent=resolved_agent or "",
        agent_path=resolved_request.agent_metadata.get("session_agent_path") or "",
        skills=resolved_request.skills,
        skill_paths=resolved_request.skill_paths,
    )
    published_launch = await asyncio.to_thread(
        service.lifecycle.start,
        SpawnReservation(
            chat_id=chat_id or "",
            owner_chat_id=chat_id or "",
            parent_id=parent_id,
            session_metadata=prepare_session_metadata,
            kind=kind,
            prompt=resolved_request.prompt,
            metadata=start_metadata,
            spawn_id=str(final_spawn_id),
            harness_session_id=resolved_request.session.requested_harness_session_id,
            control_root=launch_ctx.control_root.as_posix(),
            task_cwd=(launch_ctx.task_cwd.as_posix() if launch_ctx.task_cwd is not None else None),
            execution_cwd=launch_ctx.binding.child_cwd.as_posix(),
            launch_mode=launch_mode,
            runner_pid=runner_pid,
            launch_policy_snapshot=resolved_request.launch_policy_snapshot,
        ),
        launch_lock=launch_lock,
    )
    if published_launch != reserved_launch:
        raise RuntimeError(f"Reserved launch {reserved_launch} but published {published_launch}")
    # The spawn dir exists only now, and the bound launch may reference files in it
    # (``system-prompt.md``), so a prepared spawn is launchable without caller help.
    launch = published_launch
    write_projection_artifacts(
        launch=launch,
        launch_context=launch_ctx,
        surface="spawn",
    )

    harness_id = HarnessId(resolved_harness)
    pi_session_role = (
        resolve_pi_session_role(interactive=launch_ctx.binding.run_params.interactive)
        if harness_id is HarnessId.PI
        else None
    )
    launch_config_snapshot = launch_ctx.runtime.config_snapshot
    child_cwd = launch_ctx.binding.child_cwd
    connection_task_cwd = (
        child_cwd if child_cwd.resolve() != launch_ctx.control_root.resolve() else None
    )
    child_env = dict(launch_ctx.binding.environment.final_env)
    pi_child_wave_timeout_seconds = resolve_pi_child_wave_timeout_seconds(
        explicit_timeout_seconds=None,
        config_snapshot=launch_config_snapshot,
    )
    pi_task_ping_interval_seconds = resolve_pi_task_ping_interval_seconds(
        explicit_interval_seconds=resolved_request.pi_task_ping_interval_seconds,
        config_snapshot=launch_config_snapshot,
    )
    if harness_id is HarnessId.PI:
        assert pi_session_role is not None
        apply_pi_bind_time_env(
            child_env,
            launch_role=pi_session_role,
            timeout_seconds=pi_child_wave_timeout_seconds,
            interval_seconds=pi_task_ping_interval_seconds,
            reset_on_activity=resolved_request.pi_task_ping_reset_on_activity,
        )
    connection_config = ConnectionConfig(
        harness_id=harness_id,
        prompt=launch_ctx.binding.run_params.prompt,
        control_root=launch_ctx.control_root,
        child_env=child_env,
        launch=launch,
        task_cwd=connection_task_cwd,
        pi_child_wave_timeout_seconds=pi_child_wave_timeout_seconds,
        resident_deadline_seconds=resolve_resident_deadline_seconds(
            config_snapshot=launch_config_snapshot,
            harness_id=os.getenv("_MERIDIAN_HARNESS") or harness_id,
        ),
        resident_poll_seconds=resolve_resident_poll_seconds(
            config_snapshot=launch_config_snapshot,
        ),
        resident_rearm_budget=resolved_request.execution_policy.resident_rearm_budget,
        pi_task_ping_interval_seconds=pi_task_ping_interval_seconds,
        pi_task_ping_reset_on_activity=resolved_request.pi_task_ping_reset_on_activity,
        pi_session_role=pi_session_role,
        debug_tracer=debug_tracer,
    )

    return PreparedSpawn(
        spawn_id=final_spawn_id,
        launch=launch,
        launch_context=launch_ctx,
        connection_config=connection_config,
        resolved_model=resolved_model,
        resolved_agent=resolved_agent,
        resolved_harness=resolved_harness,
        work_id=effective_work_id,
    )
