"""Headless runner for Phase-1 streaming spawn integration."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

from pydantic import TypeAdapter

from meridian.cli.utils import require_established_project_root
from meridian.lib.bootstrap.services import (
    build_spawn_application_service,
    build_spawn_lifecycle_service_from_roots,
    prepare_for_runtime_write,
)
from meridian.lib.core.clock import RealClock
from meridian.lib.core.domain import SpawnStatus, TerminalSpawnStatus
from meridian.lib.core.native_identity import NativeIdentityError
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.connections.base import HarnessConnection
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.artifact_io import LifecycleLog, record_identity_failure
from meridian.lib.launch.constants import RUNNER_LIFECYCLE_FILENAME
from meridian.lib.launch.native_run import bind_entry, conclude_native_run
from meridian.lib.launch.process.session import build_session_metadata
from meridian.lib.launch.request import LaunchArgvIntent, SpawnRequest
from meridian.lib.launch.resolve import (
    resolve_agent_launch_input,
    resolve_startup_timeout_seconds,
)
from meridian.lib.launch.session_scope import session_scope
from meridian.lib.launch.streaming_runner import run_streaming_spawn, signal_coordinator
from meridian.lib.ops.runtime import OperationRuntime
from meridian.lib.ops.spawn.execute_init import build_spawn_mars_runtime
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
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
    agent_launch = resolve_agent_launch_input(agent)

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
        agent=agent_launch.agent,
        agent_opt_out=agent_launch.agent_opt_out,
    )
    operation_runtime = OperationRuntime.from_prepared(
        prepared,
        harness_registry=get_default_harness_registry(),
    )
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
        initial_status=SpawnStatus.RUNNING,
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

    outcome_status: TerminalSpawnStatus = "failed"
    outcome_exit_code = 1
    failure_message: str | None = None
    lifecycle_service = build_spawn_lifecycle_service_from_roots(project_root, runtime_root)
    try:
        with session_scope(
            runtime_root=runtime_root,
            metadata=build_session_metadata(launch_ctx.resolved_request),
            request=launch_ctx.resolved_request.session,
            control_root=str(launch_ctx.control_root),
            execution_cwd=str(launch_ctx.binding.child_cwd),
            spawn_id=str(spawn_id),
            startup_attempt_id=uuid.uuid4().hex,
        ) as managed:
            spawn_store.update_spawn(runtime_root, spawn_id, chat_id=managed.chat_id)
            lifecycle = LifecycleLog(
                runtime_root, spawn_id, output_path.parent / RUNNER_LIFECYCLE_FILENAME, RealClock()
            )
            try:
                native_run = bind_entry(
                    managed, launch_ctx.binding.spec, harness=str(launch_ctx.harness.id)
                )
            except NativeIdentityError as exc:
                record_identity_failure(exc, lifecycle=lifecycle, phase="pre_exec")
                raise
            connection: HarnessConnection[Any] | None = None
            identity_error: NativeIdentityError | None = None
            started_pid: int | None = None
            started_at_epoch = time.time()

            def record_started(started_connection: HarnessConnection[Any]) -> None:
                nonlocal connection, started_pid
                connection = started_connection
                started_pid = connection.subprocess_pid
                if connection.session_id:
                    native_run.observe(connection.session_id)

            connection_config = replace(
                connection_config,
                session_id_observer=native_run.observe,
                child_env={**connection_config.child_env, "MERIDIAN_CHAT_ID": managed.chat_id},
            )
            child_env = dict(connection_config.child_env)
            prelaunch = launch_ctx.harness.prepare_prelaunch(
                runtime_root=runtime_root,
                spawn_id=spawn_id,
                session=launch_ctx.resolved_request.session,
                child_cwd=launch_ctx.binding.child_cwd,
                child_env=child_env,
                resolved_harness_session_id=native_run.entry.session_id or "",
            )
            child_env.update(prelaunch.env_overrides)
            connection_config = replace(connection_config, child_env=child_env)
            try:
                outcome = await run_streaming_spawn(
                    config=connection_config,
                    spec=launch_ctx.binding.spec,
                    runtime_root=runtime_root,
                    project_root=project_root,
                    spawn_id=spawn_id,
                    startup_timeout_seconds=resolve_startup_timeout_seconds(
                        config_snapshot=launch_ctx.runtime.config_snapshot,
                    ),
                    lifecycle_service=lifecycle_service,
                    on_control_endpoint_ready=_report_control_endpoint,
                    on_running=record_started,
                )
            except NativeIdentityError as exc:
                identity_error = exc
                raise
            finally:
                native_outcome = conclude_native_run(
                    native_run,
                    launch_ctx.harness,
                    context=launch_ctx,
                    spawn_id=spawn_id,
                    child_env=connection_config.child_env,
                    child_cwd=launch_ctx.binding.child_cwd,
                    pid=started_pid,
                    started=connection is not None,
                    started_at_epoch=started_at_epoch,
                    prior_error=identity_error,
                    artifacts=LocalStore(root_dir=runtime_root / "artifacts"),
                    connection_session_id=connection.session_id if connection is not None else None,
                    lifecycle=lifecycle,
                )
                launch_ctx.harness.cleanup_prelaunch(
                    runtime_root=runtime_root,
                    spawn_id=spawn_id,
                    chat_id=managed.chat_id,
                    state=prelaunch,
                )
                if native_outcome.error is not None:
                    raise native_outcome.error
            outcome_status = TypeAdapter(TerminalSpawnStatus).validate_python(outcome.status)
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
