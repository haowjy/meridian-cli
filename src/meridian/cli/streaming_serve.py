"""Headless runner for Phase-1 streaming spawn integration."""

from __future__ import annotations

import time
from dataclasses import replace

from meridian.cli.utils import require_established_project_root
from meridian.lib.bootstrap.services import (
    build_spawn_application_service,
    build_spawn_lifecycle_service_from_roots,
    prepare_for_runtime_write,
)
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.request import LaunchArgvIntent, SpawnRequest
from meridian.lib.ops.runtime import build_runtime
from meridian.lib.ops.spawn.execute_init import build_spawn_mars_runtime
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

    prepared = prepare_for_runtime_write(require_established_project_root())
    project_root = prepared.project_root
    if prepared.runtime_root is None:
        raise ValueError("Prepared runtime write context is missing runtime root.")
    runtime_root = prepared.runtime_root
    start_monotonic = time.monotonic()
    spawn_service = build_spawn_application_service(prepared)

    # Build request and runtime BEFORE allocating spawn ID (SEAM-1)
    spawn_req = SpawnRequest(
        prompt=normalized_prompt,
        model=normalized_model,
        harness=harness_id.value,
        agent=normalized_agent,
    )
    operation_runtime = build_runtime(project_root)
    launch_runtime = build_spawn_mars_runtime(
        runtime=operation_runtime,
        runtime_root=runtime_root,
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
        debug=debug,
    )

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
        connection_config = replace(connection_config, debug_tracer=tracer)

    output_path = spawn_output_path(runtime_root, spawn_id)

    print(f"Started spawn {spawn_id} (harness={prepared.resolved_harness})")
    print(f"Events: {output_path}")

    def _report_control_endpoint(endpoint: str) -> None:
        print(f"Control endpoint: {endpoint}")

    outcome_status: SpawnStatus = "failed"
    outcome_exit_code = 1
    failure_message: str | None = None
    lifecycle_service = build_spawn_lifecycle_service_from_roots(project_root, runtime_root)
    try:
        outcome = await run_streaming_spawn(
            config=connection_config,
            spec=launch_ctx.binding.spec,
            runtime_root=runtime_root,
            project_root=project_root,
            spawn_id=spawn_id,
            lifecycle_service=lifecycle_service,
            on_control_endpoint_ready=_report_control_endpoint,
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
