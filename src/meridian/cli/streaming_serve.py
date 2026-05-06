"""Headless runner for Phase-1 streaming spawn integration."""

from __future__ import annotations

import time

from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.request import LaunchArgvIntent, LaunchRuntime, SpawnRequest
from meridian.lib.launch.streaming_runner import run_streaming_spawn, signal_coordinator
from meridian.lib.state.paths import spawn_output_path


async def streaming_serve(
    harness: str,
    prompt: str,
    model: str | None = None,
    agent: str | None = None,
    debug: bool = False,
) -> None:
    """Start a bidirectional spawn and keep it running until completion."""

    normalized_harness = harness.strip().lower()
    if not normalized_harness:
        raise ValueError("harness is required")
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        raise ValueError("prompt is required")
    normalized_model = model.strip() if model is not None else None
    if model is not None and not normalized_model:
        raise ValueError("model cannot be empty")
    normalized_agent = agent.strip() if agent is not None else None

    try:
        harness_id = HarnessId(normalized_harness)
    except ValueError as exc:
        supported = ", ".join(item.value for item in HarnessId)
        raise ValueError(f"unsupported harness '{harness}'. Supported: {supported}") from exc

    prepared = prepare_for_runtime_write(resolve_project_root())
    project_root = prepared.project_root
    if prepared.runtime_root is None:
        raise ValueError("Prepared runtime write context is missing runtime root.")
    runtime_root = prepared.runtime_root
    start_monotonic = time.monotonic()
    lifecycle = create_lifecycle_service(project_root, runtime_root)
    spawn_service = SpawnApplicationService(runtime_root, lifecycle)

    # Build request and runtime BEFORE allocating spawn ID (SEAM-1)
    spawn_req = SpawnRequest(
        prompt=normalized_prompt,
        model=normalized_model,
        harness=harness_id.value,
        agent=normalized_agent,
    )
    launch_runtime = LaunchRuntime(
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
        runtime_root=runtime_root.as_posix(),
        project_paths_project_root=project_root.as_posix(),
        project_paths_execution_cwd=project_root.as_posix(),
    )

    tracer = None
    if debug:
        from meridian.lib.observability.debug_tracer import DebugTracer

        # Tracer needs spawn_id, but we need tracer for prepare_spawn.
        # Solution: defer tracer creation until after prepare_spawn, then
        # update connection_config if needed. Or pass it into prepare_spawn.
        # For now, pass debug_tracer=None and create it after.
        tracer = None  # Will set after we have spawn_id

    # Resolve-before-persist: prepare_spawn builds launch context first,
    # then atomically creates the row with real metadata (SEAM-1, SEAM-2)
    # ConnectionConfig projected from LaunchContext (DS-002)
    prepared = await spawn_service.prepare_spawn(
        request=spawn_req,
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
        kind="streaming",
        launch_mode="foreground",
        initial_status="running",
    )
    spawn_id = prepared.spawn_id
    connection_config = prepared.connection_config
    launch_ctx = prepared.launch_context

    # Now create debug tracer with actual spawn_id if requested
    if debug:
        from meridian.lib.observability.debug_tracer import DebugTracer

        spawn_dir = runtime_root / "spawns" / str(spawn_id)
        tracer = DebugTracer(
            spawn_id=str(spawn_id),
            debug_path=spawn_dir / "debug.jsonl",
            echo_stderr=True,
        )
        # Update connection_config with tracer - need to create a new one
        # since ConnectionConfig is frozen
        from meridian.lib.harness.connections.base import ConnectionConfig

        connection_config = ConnectionConfig(
            spawn_id=connection_config.spawn_id,
            harness_id=connection_config.harness_id,
            prompt=connection_config.prompt,
            project_root=connection_config.project_root,
            env_overrides=connection_config.env_overrides,
            system=connection_config.system,
            timeout_seconds=connection_config.timeout_seconds,
            ws_bind_host=connection_config.ws_bind_host,
            ws_port=connection_config.ws_port,
            debug_tracer=tracer,
        )

    output_path = spawn_output_path(runtime_root, spawn_id)
    socket_path = runtime_root / "spawns" / str(spawn_id) / "control.sock"

    print(f"Started spawn {spawn_id} (harness={prepared.resolved_harness})")
    print(f"Control socket: {socket_path}")
    print(f"Events: {output_path}")

    outcome_status: SpawnStatus = "failed"
    outcome_exit_code = 1
    failure_message: str | None = None
    try:
        outcome = await run_streaming_spawn(
            config=connection_config,
            spec=launch_ctx.spec,
            runtime_root=runtime_root,
            project_root=project_root,
            spawn_id=spawn_id,
        )
        outcome_status = outcome.status
        outcome_exit_code = outcome.exit_code
        if outcome_status == "failed":
            failure_message = outcome.error
    except Exception as exc:
        failure_message = str(exc)
        raise
    finally:
        with signal_coordinator().mask_sigterm():
            finalize_outcome = await spawn_service.complete_spawn(
                spawn_id,
                status=outcome_status,
                exit_code=outcome_exit_code,
                origin="launcher",
                duration_secs=max(0.0, time.monotonic() - start_monotonic),
                error=failure_message if outcome_status == "failed" else None,
            )
            if not finalize_outcome.wrote:
                print(f"Finalize skipped for spawn {spawn_id} (already terminal or missing)")
            print(f"Stopped spawn {spawn_id} (status={outcome_status}, exit={outcome_exit_code})")
