"""Process launch orchestration."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
from meridian.lib.catalog.model_aliases import MarsResultCache
from meridian.lib.core.domain import SpawnStatus, TokenUsage
from meridian.lib.core.spawn_lifecycle import (
    ExecutionTerminalFacts,
    SpawnReservation,
    has_durable_report_completion,
)
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import (
    ForkMaterializationMode,
    HarnessContract,
    HarnessPrelaunchState,
    NativePrimaryRuntimeMetadata,
    SubprocessHarness,
)
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.connections import get_connection_class
from meridian.lib.harness.connections.base import (
    HarnessConnection,
    PrimaryRuntimeEventSurface,
    PrimaryRuntimeRequestPolicy,
    RawHarnessEvent,
)
from meridian.lib.harness.cost import estimate_usage_cost
from meridian.lib.harness.passthrough import get_passthrough
from meridian.lib.harness.passthrough.base import PassthroughError
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.artifact_io import write_projection_artifacts
from meridian.lib.launch.constants import (
    HISTORY_FILENAME,
    OUTPUT_FILENAME,
    PRIMARY_META_FILENAME,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import InMemoryStore, LocalStore, make_artifact_key
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.primary_meta import (
    ActivityState,
    HarnessSessionDiscovery,
    PrimaryMetadata,
    write_primary_metadata,
)
from meridian.lib.state.session_store import (
    NativeBindingResult,
    get_session_active_work_id,
    start_session,
    stop_session,
    update_session_claude_config_dir,
    update_session_harness_id,
    update_session_spawn_id,
    update_session_work_id,
)
from meridian.lib.state.spawn.model import FOREGROUND_LAUNCH_MODE

from ..context import (
    LaunchContext,
    PreparedLaunchSurface,
    RuntimeBindings,
    bind_launch_context,
    build_launch_context,
)
from ..fork import materialize_fork
from ..request import LaunchCompositionSurface
from ..session_scope import bind_harness_session_id, session_scope
from ..types import SessionMode
from .ports import (
    PRIMARY_STDERR_LOG_PATH_ENV,
    ProcessBackendId,
    ProcessLauncher,
    ProcessLauncherSelector,
    ProcessPlatformContract,
    ProcessSurfaceMode,
    SelectedProcessLauncher,
)
from .primary_attach import PrimaryAttachError, PrimaryAttachLauncher, PrimaryAttachOutcome
from .pty_launcher import PtyProcessLauncher, can_use_pty
from .session import (
    build_session_metadata,
    resolve_attached_work_id,
    resolve_primary_session_mode,
)
from .subprocess_launcher import SubprocessProcessLauncher
from .windows_launcher import WindowsConsoleLauncher, can_use_windows_console_launcher

logger = logging.getLogger(__name__)


class ProcessOutcome(BaseModel):
    """Result of running the harness subprocess."""

    model_config = ConfigDict(frozen=True)

    command: tuple[str, ...]
    exit_code: int
    chat_id: str | None
    primary_spawn_id: str | None
    primary_started: float
    primary_started_epoch: float
    primary_started_local_iso: str | None
    resolved_harness_session_id: str


def _write_native_primary_metadata(
    *,
    runtime_root: Path,
    spawn_id: SpawnId,
    spawn_dir: Path,
    command: tuple[str, ...],
    launch_cwd: Path,
    launcher_pid: int,
    tui_pid: int | None,
    activity: ActivityState | None,
    started_at_epoch: float | None,
    ended_at_epoch: float | None,
    exit_code: int | None,
    harness_session_id: str | None,
    runtime_metadata: NativePrimaryRuntimeMetadata | None = None,
    harness_session_discovery: HarnessSessionDiscovery | None = None,
    harness_session_discovery_detail: str | None = None,
) -> None:
    """Best-effort metadata projection for native/black-box primary launches."""

    try:
        write_primary_metadata(
            spawn_dir,
            PrimaryMetadata(
                managed_backend=False,
                launcher_pid=launcher_pid,
                backend_pid=None,
                tui_pid=tui_pid,
                backend_port=None,
                activity=activity,
                harness_session_id=(harness_session_id or "").strip() or None,
                harness_session_discovery=harness_session_discovery,
                harness_session_discovery_detail=(
                    (harness_session_discovery_detail or "").strip() or None
                ),
                command=command,
                launch_cwd=str(launch_cwd),
                started_at_epoch=started_at_epoch,
                ended_at_epoch=ended_at_epoch,
                exit_code=exit_code,
                runtime_kind=runtime_metadata.runtime_kind if runtime_metadata else None,
                runtime_path=runtime_metadata.runtime_path if runtime_metadata else None,
                runtime_version=runtime_metadata.runtime_version if runtime_metadata else None,
                session_dir=runtime_metadata.session_dir if runtime_metadata else None,
                auth_policy=runtime_metadata.auth_policy if runtime_metadata else None,
            ),
            runtime_root=runtime_root,
            spawn_id=str(spawn_id),
        )
    except Exception:
        logger.debug("Failed to write native primary metadata", exc_info=True)


RunPrimaryProcessWithCapture = Callable[
    [tuple[str, ...], Path, dict[str, str], Path | None, Callable[[int], None] | None],
    tuple[int, int | None],
]
RunPrimaryAttach = Callable[
    [
        HarnessId,
        SpawnId,
        Path,
        Path,
        Path | None,
        dict[str, str],
        ResolvedLaunchSpec,
        ProcessLauncher,
        Callable[[int], None] | None,
    ],
    PrimaryAttachOutcome,
]


def select_process_launcher(output_log_path: Path | None) -> ProcessLauncher:
    """Choose the launch backend for one primary process invocation."""

    return select_process_backend(output_log_path).launcher


def select_process_backend(output_log_path: Path | None) -> SelectedProcessLauncher:
    """Choose the launch backend plus explicit process/platform contract."""

    if output_log_path is not None:
        if can_use_pty():
            return SelectedProcessLauncher(
                launcher=PtyProcessLauncher(),
                contract=ProcessPlatformContract(
                    backend_id=ProcessBackendId.PTY,
                    surface_mode=ProcessSurfaceMode.PTY_MEDIATED,
                    captures_output_to_artifact=True,
                    platform_family="posix",
                ),
            )
        return SelectedProcessLauncher(
            launcher=SubprocessProcessLauncher(),
            contract=ProcessPlatformContract(
                backend_id=ProcessBackendId.SUBPROCESS,
                surface_mode=ProcessSurfaceMode.PIPE_CAPTURE,
                captures_output_to_artifact=True,
                platform_family="portable",
            ),
        )
    if can_use_windows_console_launcher():
        return SelectedProcessLauncher(
            launcher=WindowsConsoleLauncher(),
            contract=ProcessPlatformContract(
                backend_id=ProcessBackendId.WINDOWS_CONSOLE,
                surface_mode=ProcessSurfaceMode.NATIVE_INHERIT,
                captures_output_to_artifact=False,
                platform_family="windows",
            ),
        )
    if can_use_pty():
        return SelectedProcessLauncher(
            launcher=PtyProcessLauncher(),
            contract=ProcessPlatformContract(
                backend_id=ProcessBackendId.PTY,
                surface_mode=ProcessSurfaceMode.PTY_MEDIATED,
                captures_output_to_artifact=False,
                platform_family="posix",
            ),
        )
    return SelectedProcessLauncher(
        launcher=SubprocessProcessLauncher(),
        contract=ProcessPlatformContract(
            backend_id=ProcessBackendId.SUBPROCESS,
            surface_mode=ProcessSurfaceMode.NATIVE_INHERIT,
            captures_output_to_artifact=False,
            platform_family="portable",
        ),
    )


def run_primary_process_with_capture(
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    output_log_path: Path | None,
    on_child_started: Callable[[int], None] | None = None,
    *,
    launcher_selector: ProcessLauncherSelector = select_process_launcher,
) -> tuple[int, int | None]:
    launcher: ProcessLauncher = launcher_selector(output_log_path)

    running = launcher.start(
        command=command,
        cwd=cwd,
        env=env,
        output_log_path=output_log_path,
    )
    try:
        if on_child_started is not None:
            on_child_started(running.pid)
    except Exception:
        running.terminate()
        running.wait()
        raise
    launched = running.wait()
    return launched.exit_code, launched.pid


def _cleanup_managed_primary_sidecars(spawn_dir: Path) -> None:
    """Delete managed sidecars when attach startup falls back to black-box launch."""

    for filename in (
        PRIMARY_META_FILENAME,
        OUTPUT_FILENAME,
    ):
        with suppress(OSError):
            (spawn_dir / filename).unlink()


def _managed_primary_stderr_excerpt(spawn_dir: Path, *, max_chars: int = 1200) -> str | None:
    stderr_path = spawn_dir / "stderr.log"
    max_bytes = max(max_chars * 4, 4096)
    try:
        file_size = stderr_path.stat().st_size
        with stderr_path.open("rb") as handle:
            truncated = file_size > max_bytes
            if truncated:
                handle.seek(-max_bytes, os.SEEK_END)
            data = handle.read(max_bytes)
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    excerpt = text.strip()
    if not excerpt:
        return None
    if truncated:
        excerpt = f"…{excerpt}"
        if len(excerpt) > max_chars:
            return f"…{excerpt[-max_chars:].lstrip()}"
    if len(excerpt) <= max_chars:
        return excerpt
    return f"{excerpt[:max_chars].rstrip()}…"


def _execute_via_managed_attach(
    *,
    harness_id: HarnessId,
    primary_spawn_id: SpawnId,
    log_dir: Path,
    control_root: Path,
    task_cwd: Path | None,
    child_env: dict[str, str],
    launch_spec: ResolvedLaunchSpec,
    managed: Any,
    runtime_root: Path,
    run_primary_attach_fn: RunPrimaryAttach,
    on_running: Callable[[int], None],
) -> tuple[int, str | None, bool]:
    """Run managed attach path and persist managed session id when available."""

    managed_outcome = run_primary_attach_fn(
        harness_id,
        primary_spawn_id,
        log_dir,
        control_root,
        task_cwd,
        child_env,
        launch_spec,
        select_process_launcher(None),
        on_running,
    )
    managed_session_id = bind_harness_session_id(
        runtime_root=runtime_root,
        spawn_id=primary_spawn_id,
        record_session_id=managed.record_harness_session_id,
        session_id=managed_outcome.session_id,
        source="observed",
    )
    return (
        managed_outcome.exit_code,
        managed_session_id or None,
        managed_outcome.cancelled,
    )


def _execute_via_blackbox(
    *,
    command: tuple[str, ...],
    launch_cwd: Path,
    child_env: dict[str, str],
    output_log_path: Path | None,
    run_primary_process_with_capture_fn: RunPrimaryProcessWithCapture,
    on_running: Callable[[int], None],
) -> int:
    """Run legacy black-box launch path and return process exit code."""

    exit_code, _child_pid = run_primary_process_with_capture_fn(
        command,
        launch_cwd,
        child_env,
        output_log_path,
        on_running,
    )
    return exit_code


def _persist_blackbox_output_artifact(
    *,
    artifacts: LocalStore,
    spawn_id: SpawnId | None,
    log_dir: Path,
) -> None:
    """Mirror captured primary black-box output into the artifact store."""

    if spawn_id is None:
        return
    output_path = log_dir / OUTPUT_FILENAME
    if not output_path.is_file():
        return
    with suppress(OSError):
        artifacts.put(
            make_artifact_key(spawn_id, OUTPUT_FILENAME),
            output_path.read_bytes(),
        )


def _execute_primary_process(
    *,
    harness_id: HarnessId,
    primary_spawn_id: SpawnId,
    log_dir: Path,
    control_root: Path,
    launch_cwd: Path,
    task_cwd: Path | None,
    child_env: dict[str, str],
    launch_spec: ResolvedLaunchSpec,
    command: tuple[str, ...],
    harness_contract: HarnessContract,
    managed: Any,
    runtime_root: Path,
    run_primary_process_with_capture_fn: RunPrimaryProcessWithCapture,
    run_primary_attach_fn: RunPrimaryAttach,
    on_running: Callable[[int], None],
) -> tuple[int, str | None, bool]:
    """Run managed attach when eligible, otherwise fall back to black-box launch."""

    use_managed_backend = harness_contract.bootstrap.mode.value == "managed_primary_attach"
    if use_managed_backend:
        try:
            exit_code, managed_session_id, cancelled = _execute_via_managed_attach(
                harness_id=harness_id,
                primary_spawn_id=primary_spawn_id,
                log_dir=log_dir,
                control_root=control_root,
                task_cwd=task_cwd,
                child_env=child_env,
                launch_spec=launch_spec,
                managed=managed,
                runtime_root=runtime_root,
                run_primary_attach_fn=run_primary_attach_fn,
                on_running=on_running,
            )
            return exit_code, managed_session_id, cancelled
        except PrimaryAttachError as exc:
            if harness_contract.bootstrap.primary_attach_failure_policy == "raise":
                raise
            stderr_excerpt = _managed_primary_stderr_excerpt(log_dir)
            logger.warning(
                "Managed backend failed, falling back to black-box TUI: %s%s",
                exc,
                f"\nManaged backend stderr excerpt:\n{stderr_excerpt}"
                if stderr_excerpt is not None
                else "",
            )
            _cleanup_managed_primary_sidecars(log_dir)

    output_log_path = (
        log_dir / OUTPUT_FILENAME
        if (
            harness_contract.capabilities.captures_blackbox_output
            and "--print" in command
        )
        else None
    )
    blackbox_env = dict(child_env)
    if harness_contract.bootstrap.primary_stderr_log:
        blackbox_env[PRIMARY_STDERR_LOG_PATH_ENV] = str(log_dir / "stderr.log")
    return (
        _execute_via_blackbox(
            command=command,
            launch_cwd=launch_cwd,
            child_env=blackbox_env,
            output_log_path=output_log_path,
            run_primary_process_with_capture_fn=run_primary_process_with_capture_fn,
            on_running=on_running,
        ),
        None,
        False,
    )


def _finalize_lifecycle_and_observe_session(
    *,
    primary_spawn_id: SpawnId | None,
    exit_code: int,
    resolved_harness_session_id: str,
    expected_harness_session_id: str,
    harness_adapter: Any,
    artifacts: LocalStore,
    project_root: Path,
    launch_child_cwd: Path,
    model_id: str | None,
    runtime_root: Path,
    primary_started: float,
    primary_started_epoch: float,
    primary_started_local_iso: str | None,
    managed: Any,
    spawn_service: SpawnApplicationService,
    observe_adapter_session_id: bool = True,
    cancellation_observed: bool = False,
    native_identity_error: str | None = None,
) -> tuple[int, str]:
    """Finalize lifecycle, observe identity, and durably bind accepted selections."""

    resolved_exit_code = exit_code
    if primary_spawn_id is not None:
        log_dir = resolve_spawn_log_dir(
            project_root, primary_spawn_id, runtime_root=runtime_root
        )
        report_path = log_dir / "report.md"
        try:
            report_text = report_path.read_text(encoding="utf-8") if report_path.is_file() else None
        except OSError:
            report_text = None
        duration = max(0.0, time.monotonic() - primary_started) if primary_started > 0.0 else None
        durable_report_completion = has_durable_report_completion(report_text)
        usage = _extract_primary_usage(
            harness_adapter=harness_adapter,
            primary_spawn_id=primary_spawn_id,
            project_root=project_root,
            model_id=model_id,
            log_dir=log_dir,
        )
        execution_outcome = asyncio.run(
            spawn_service.complete_execution(
                primary_spawn_id,
                ExecutionTerminalFacts(
                    exit_code=exit_code,
                    failure_reason=(
                        native_identity_error or ("cancelled" if cancellation_observed else None)
                    ),
                    cancellation_observed=cancellation_observed,
                    durable_report_completion=(
                        durable_report_completion and native_identity_error is None
                    ),
                ),
                origin="launcher",
                duration_secs=duration,
                usage=usage,
            )
        )
        resolved_exit_code = execution_outcome.resolved.exit_code
        outcome = execution_outcome.completion
        if not outcome.wrote:
            logger.info(
                "Launcher finalize skipped; spawn already terminal or missing: %s",
                primary_spawn_id,
            )
    observed_harness_session_id = None
    try:
        if observe_adapter_session_id and primary_started_epoch > 0.0:
            observed_harness_session_id = harness_adapter.observe_session_id(
                artifacts=artifacts,
                spawn_id=primary_spawn_id,
                current_session_id=resolved_harness_session_id,
                project_root=launch_child_cwd,
                started_at_epoch=primary_started_epoch,
                started_at_local_iso=primary_started_local_iso,
                expected_session_id=expected_harness_session_id,
            )
    except Exception:
        logger.debug("Best-effort harness session observation failed", exc_info=True)
    resolved_harness_session_id = bind_harness_session_id(
        runtime_root=runtime_root,
        spawn_id=primary_spawn_id,
        record_session_id=managed.record_harness_session_id,
        session_id=observed_harness_session_id,
        source="observed",
        current_session_id=resolved_harness_session_id,
        chat_id=managed.chat_id,
    )
    return resolved_exit_code, resolved_harness_session_id


def _extract_primary_usage(
    *,
    harness_adapter: Any,
    primary_spawn_id: SpawnId,
    project_root: Path,
    model_id: str | None,
    log_dir: Path,
) -> TokenUsage | None:
    """Best-effort usage extraction for primary-session finalization."""

    try:
        usage_artifacts = InMemoryStore()
        for filename in (HISTORY_FILENAME, OUTPUT_FILENAME):
            source = log_dir / filename
            if not source.is_file():
                continue
            try:
                usage_artifacts.put(
                    make_artifact_key(primary_spawn_id, filename),
                    source.read_bytes(),
                )
            except Exception:
                logger.debug(
                    "Failed to mirror primary artifact for usage extraction",
                    exc_info=True,
                )
        raw_usage = harness_adapter.extract_usage(usage_artifacts, primary_spawn_id)
        usage = estimate_usage_cost(
            model_id=(model_id or "").strip() or None,
            usage=raw_usage,
            project_root=project_root,
            harness_id=str(harness_adapter.id),
        )
        if all(
            value is None
            for value in (
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_input_tokens,
                usage.cache_creation_input_tokens,
                usage.reasoning_tokens,
                usage.total_cost_usd,
            )
        ):
            return None
        return usage
    except Exception:
        logger.debug("Best-effort primary usage extraction failed", exc_info=True)
        return None


def _create_managed_primary_connection(
    *,
    connection_factory: Callable[..., HarnessConnection[Any]],
    harness_contract: HarnessContract,
    harness_adapter: SubprocessHarness,
    spawn_dir: Path,
) -> HarnessConnection[Any]:
    """Build one managed-primary connection configured from harness contract data."""

    connection_ref: dict[str, HarnessConnection[Any]] = {}

    policy = harness_contract.approval.primary_session_runtime_request_policy
    event_surface = harness_contract.approval.primary_session_runtime_event_surface

    connection = connection_factory()
    connection_ref["connection"] = connection
    if policy is PrimaryRuntimeRequestPolicy.SURFACE_EVENTS:
        if event_surface is not PrimaryRuntimeEventSurface.CONNECTION_EVENT_STREAM:
            raise PrimaryAttachError(
                "Managed primary runtime request surfacing requires connection event stream"
            )

        async def _event_sink(event: RawHarnessEvent) -> None:
            await connection_ref["connection"].inject_runtime_event(event)

        connection.configure_primary_runtime_requests(
            policy=policy,
            event_sink=_event_sink,
            request_handler=harness_adapter.build_primary_runtime_request_handler(
                spawn_dir=spawn_dir,
                event_sink=_event_sink,
            ),
        )
        return connection

    connection.configure_primary_runtime_requests(policy=policy)
    return connection


async def _run_primary_attach(
    *,
    harness_id: HarnessId,
    spawn_id: SpawnId,
    spawn_dir: Path,
    control_root: Path,
    task_cwd: Path | None,
    env: dict[str, str],
    spec: ResolvedLaunchSpec,
    process_launcher: ProcessLauncher,
    on_running: Callable[[int], None] | None = None,
) -> PrimaryAttachOutcome:
    """Launch managed backend + primary TUI attach flow for supported harnesses."""

    try:
        passthrough = get_passthrough(harness_id)
        harness_bundle = get_harness_bundle(harness_id)
        harness_adapter = cast("SubprocessHarness", harness_bundle.adapter)
        connection_factory = cast(
            "Callable[..., HarnessConnection[Any]]",
            get_connection_class(harness_id),
        )
        connection = _create_managed_primary_connection(
            connection_factory=connection_factory,
            harness_contract=harness_adapter.contract,
            harness_adapter=harness_adapter,
            spawn_dir=spawn_dir,
        )
        config = passthrough.build_config(
            spawn_id=spawn_id,
            spec=spec,
            control_root=control_root,
            task_cwd=task_cwd,
            env=env,
        )
        launcher = PrimaryAttachLauncher(
            spawn_id=spawn_id,
            spawn_dir=spawn_dir,
            connection=connection,
            tui_command_builder=passthrough.build_tui_command(connection, spec),
            process_launcher=process_launcher,
            runtime_root=spawn_dir.parent.parent,
            on_running=on_running,
        )
        return await launcher.run(
            config=config,
            spec=spec,
            cwd=control_root,
            env=env,
        )
    except PrimaryAttachError:
        raise
    except PassthroughError as exc:
        raise PrimaryAttachError(str(exc)) from exc
    except Exception as exc:
        raise PrimaryAttachError(
            f"Managed primary attach failed for {harness_id.value}: {exc}"
        ) from exc


def run_primary_attach(
    harness_id: HarnessId,
    spawn_id: SpawnId,
    spawn_dir: Path,
    control_root: Path,
    task_cwd: Path | None,
    env: dict[str, str],
    spec: ResolvedLaunchSpec,
    process_launcher: ProcessLauncher,
    on_running: Callable[[int], None] | None = None,
) -> PrimaryAttachOutcome:
    """Run managed primary attach lifecycle from sync runner code."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _run_primary_attach(
                harness_id=harness_id,
                spawn_id=spawn_id,
                spawn_dir=spawn_dir,
                control_root=control_root,
                task_cwd=task_cwd,
                env=env,
                spec=spec,
                process_launcher=process_launcher,
                on_running=on_running,
            )
        )
    raise PrimaryAttachError("Managed primary attach cannot run inside an active event loop")


def run_harness_process(
    launch_context: LaunchContext,
    harness_registry: HarnessRegistry,
    *,
    prepared: PreparedLaunchSurface | None = None,
    cache: MarsResultCache | None = None,
    run_primary_process_with_capture_fn: RunPrimaryProcessWithCapture = (
        run_primary_process_with_capture
    ),
    run_primary_attach_fn: RunPrimaryAttach = run_primary_attach,
    start_session_fn: Callable[..., str] = start_session,
    stop_session_fn: Callable[..., None] = stop_session,
    update_session_harness_id_fn: Callable[..., NativeBindingResult | None] = (
        update_session_harness_id
    ),
    update_session_spawn_id_fn: Callable[..., None] = update_session_spawn_id,
    update_session_work_id_fn: Callable[..., None] = update_session_work_id,
    get_session_active_work_id_fn: Callable[[Path, str], str | None] = get_session_active_work_id,
) -> ProcessOutcome:
    """Start session, spawn tracking, launch process, wait for exit."""

    config_root = launch_context.project_root
    control_root = launch_context.control_root
    task_cwd = launch_context.task_cwd
    execution_cwd = launch_context.execution_cwd
    runtime_root = launch_context.runtime_root
    preview_context = launch_context
    command = preview_context.binding.argv
    spawn_request = preview_context.request
    preview_request = preview_context.resolved_request
    requested_harness_session_id = (
        preview_request.session.requested_harness_session_id or ""
    ).strip()
    session_mode = resolve_primary_session_mode(preview_context)
    session_metadata = build_session_metadata(preview_request)
    expected_harness_session_id = (
        preview_context.binding.effective_harness_session_id or ""
    ) if session_mode != SessionMode.FORK else ""
    resolved_harness_session_id = (
        expected_harness_session_id if session_mode == SessionMode.RESUME else ""
    )
    harness_adapter = preview_context.harness
    harness_id = HarnessId(session_metadata.harness)
    chat_id: str | None = None
    primary_spawn_id: SpawnId | None = None
    primary_started = 0.0
    primary_started_epoch = 0.0
    primary_started_local_iso: str | None = None
    launch_child_cwd = control_root
    prelaunch_state = HarnessPrelaunchState()
    identity_plan = None
    artifacts = LocalStore(root_dir=runtime_root / "artifacts")
    spawn_service = build_spawn_application_service_from_roots(config_root, runtime_root)
    lifecycle_service = spawn_service.lifecycle

    resume_chat_id = (
        preview_request.session.continue_chat_id if session_mode == SessionMode.RESUME else None
    )
    exit_code = 2
    managed_cancelled = False
    native_primary_tui_pid: int | None = None
    write_native_primary_metadata = False
    native_primary_metadata_command: tuple[str, ...] = command
    native_primary_runtime_metadata = NativePrimaryRuntimeMetadata()
    child_env: dict[str, str] = {}
    startup_attempt_id = uuid.uuid4().hex
    try:
        with session_scope(
            runtime_root=runtime_root,
            metadata=session_metadata,
            request=preview_request.session,
            harness_session_id=resolved_harness_session_id,
            chat_id=resume_chat_id,
            control_root=str(control_root),
            task_cwd=task_cwd.as_posix() if task_cwd is not None else None,
            execution_cwd=str(execution_cwd),
            kind="primary",
            startup_attempt_id=startup_attempt_id,
            _start_session=start_session_fn,
            _stop_session=stop_session_fn,
            _update_session_harness_id=update_session_harness_id_fn,
        ) as managed:
            chat_id = managed.chat_id
            attached_work_id = resolve_attached_work_id(
                runtime_root=runtime_root,
                chat_id=chat_id,
                explicit_work_id=preview_context.work_id,
                resume_chat_id=resume_chat_id,
                get_session_active_work_id_fn=get_session_active_work_id_fn,
                update_session_work_id_fn=update_session_work_id_fn,
            )
            try:
                write_native_primary_metadata = False
                should_fork = (
                    session_mode == SessionMode.FORK
                    and harness_adapter.contract.bootstrap.fork_materialization
                    is ForkMaterializationMode.MERIDIAN_MATERIALIZED_FORK
                    and bool((preview_request.session.requested_harness_session_id or "").strip())
                )
                primary_spawn_id = SpawnId(
                    lifecycle_service.start(
                        SpawnReservation(
                            chat_id=chat_id,
                            session_metadata=session_metadata,
                            kind="primary",
                            prompt=preview_request.prompt,
                            harness_session_id=(
                                None if should_fork else resolved_harness_session_id
                            ),
                            control_root=str(control_root),
                            task_cwd=None,
                            execution_cwd=str(execution_cwd),
                            launch_mode=FOREGROUND_LAUNCH_MODE,
                            work_id=attached_work_id,
                            runner_pid=os.getpid(),
                            status=SpawnStatus.QUEUED,
                        )
                    )
                )
                update_session_spawn_id_fn(
                    runtime_root,
                    chat_id,
                    str(primary_spawn_id),
                )
                forked_session_id: str | None = None
                if should_fork:
                    source_session_id = (
                        preview_request.session.requested_harness_session_id or ""
                    ).strip()
                    forked_session_id = materialize_fork(
                        adapter=harness_adapter,
                        source_session_id=source_session_id,
                        runtime_root=runtime_root,
                        spawn_id=primary_spawn_id,
                    )
                    if prepared is None:
                        spawn_request = spawn_request.model_copy(
                            update={
                                "session": spawn_request.session.model_copy(
                                    update={
                                        "requested_harness_session_id": forked_session_id,
                                        "continue_fork": False,
                                    }
                                )
                            }
                        )
                    resolved_harness_session_id = forked_session_id
                if forked_session_id:
                    expected_harness_session_id = forked_session_id
                log_dir = resolve_spawn_log_dir(
                    config_root, primary_spawn_id, runtime_root=runtime_root
                )
                primary_started = time.monotonic()
                primary_started_epoch = time.time()
                primary_started_local_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                preview_seed_args = preview_context.binding.seed_harness_session_args
                runtime_request = spawn_request.model_copy(
                    update={
                        "extra_args": (*spawn_request.extra_args, *preview_seed_args),
                        "work_id_hint": attached_work_id,
                    }
                )
                runtime = preview_context.runtime.model_copy(
                    update={
                        "composition_surface": LaunchCompositionSurface.PRIMARY,
                        "runtime_root": runtime_root.as_posix(),
                        "config_root": config_root.as_posix(),
                        "control_root": control_root.as_posix(),
                        "requested_task_cwd": (
                            task_cwd.as_posix() if task_cwd is not None else None
                        ),
                        # Legacy aliases.
                        "project_paths_project_root": config_root.as_posix(),
                        "project_paths_execution_cwd": execution_cwd.as_posix(),
                    }
                )
                plan_overrides: dict[str, str] = {}
                config_env = runtime.config_snapshot.get("env")
                if isinstance(config_env, dict):
                    for k, v in cast("dict[str, str]", config_env).items():
                        if k.strip():
                            plan_overrides[k] = v
                if runtime_request.execution_policy.autocompact is not None:
                    plan_overrides["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(
                        runtime_request.execution_policy.autocompact
                    )
                if prepared is not None:
                    runtime_context = bind_launch_context(
                        prepared=prepared,
                        bindings=RuntimeBindings(
                            spawn_id=str(primary_spawn_id),
                            runtime_work_id=attached_work_id,
                            chat_id=chat_id,
                            forked_harness_session_id=forked_session_id,
                            continue_fork_override=False if should_fork else None,
                            plan_overrides=plan_overrides,
                        ),
                        runtime=runtime,
                        project_root=config_root,
                        harness_registry=harness_registry,
                    )
                else:
                    runtime_context = build_launch_context(
                        spawn_id=str(primary_spawn_id),
                        request=runtime_request,
                        runtime=runtime,
                        harness_registry=harness_registry,
                        plan_overrides=plan_overrides,
                        runtime_work_id=attached_work_id,
                        forked_harness_session_id=forked_session_id,
                        cache=cache,
                    )
                write_projection_artifacts(
                    log_dir=log_dir,
                    launch_context=runtime_context,
                    surface="primary",
                )
                command = runtime_context.binding.argv
                resolved_harness_session_id = (
                    runtime_context.binding.effective_harness_session_id or ""
                ) if session_mode == SessionMode.RESUME or forked_session_id else ""
                persisted_launch_policy_snapshot = (
                    runtime_context.resolved_request.launch_policy_snapshot
                )
                if persisted_launch_policy_snapshot is not None:
                    # Persist resolved launch contract for idempotent primary --continue.
                    spawn_store.update_spawn(
                        runtime_root,
                        primary_spawn_id,
                        launch_policy_snapshot=persisted_launch_policy_snapshot,
                    )
                launch_child_cwd = runtime_context.binding.child_cwd
                child_env = dict(runtime_context.binding.environment.final_env)
                if managed.chat_id:
                    child_env["MERIDIAN_CHAT_ID"] = managed.chat_id
                selected_task_cwd = runtime_context.task_cwd
                spawn_store.update_spawn(
                    runtime_root,
                    primary_spawn_id,
                    control_root=control_root.as_posix(),
                    task_cwd=(
                        selected_task_cwd.as_posix()
                        if (
                            selected_task_cwd is not None
                            and selected_task_cwd.resolve() != control_root.resolve()
                        )
                        else None
                    ),
                    execution_cwd=runtime_context.binding.child_cwd.as_posix(),
                )
                lifecycle_service.bootstrap_from_disk(str(primary_spawn_id))
                launch_spec = runtime_context.binding.spec
                identity_plan = launch_spec.native_identity_plan
                if identity_plan is not None and managed.attempt is not None:
                    attempt = replace(managed.attempt, native_store=identity_plan.native_store)
                    managed = replace(
                        managed, attempt=attempt,
                        record_harness_session_id=attempt.record_harness_session_id,
                    )
                if identity_plan is not None and identity_plan.harness_session_id:
                    result = update_session_harness_id(
                        runtime_root, managed.chat_id, identity_plan.harness_session_id or "",
                        native_store=identity_plan.native_store, source="assigned",
                        session_instance_id=(
                            managed.attempt.session_instance_id if managed.attempt else None
                        ),
                        startup_attempt_id=(
                            managed.attempt.startup_attempt_id if managed.attempt else None
                        ),
                    )
                    if result.status == "conflict":
                        raise ValueError(f"{managed.chat_id}: native binding conflict before exec")
                    resolved_harness_session_id = bind_harness_session_id(
                        runtime_root=runtime_root, spawn_id=primary_spawn_id,
                        record_session_id=lambda _: result,
                        session_id=result.harness_session_id, source="assigned",
                    )
                    expected_harness_session_id = resolved_harness_session_id
                    lifecycle_service.bootstrap_from_disk(str(primary_spawn_id))

                def _record_effective_config_dir(config_dir: str) -> None:
                    spawn_store.update_spawn(
                        runtime_root,
                        primary_spawn_id,
                        claude_config_dir=config_dir,
                    )
                    if managed.chat_id:
                        update_session_claude_config_dir(
                            runtime_root,
                            managed.chat_id,
                            claude_config_dir=config_dir,
                        )

                prelaunch_state = harness_adapter.prepare_prelaunch(
                    runtime_root=runtime_root,
                    spawn_id=primary_spawn_id,
                    session=preview_request.session,
                    child_cwd=launch_child_cwd,
                    child_env=child_env,
                    resolved_harness_session_id=resolved_harness_session_id,
                    record_effective_config_dir=_record_effective_config_dir,
                )
                if prelaunch_state.env_overrides:
                    child_env.update(prelaunch_state.env_overrides)
                command = harness_adapter.resolve_primary_command(
                    command,
                    state=prelaunch_state,
                )
                write_native_primary_metadata = harness_adapter.uses_native_primary_metadata()
                native_primary_runtime_metadata = (
                    harness_adapter.native_primary_runtime_metadata(prelaunch_state)
                )
                native_primary_metadata_command = (
                    harness_adapter.redact_primary_command(command)
                    if write_native_primary_metadata
                    else command
                )
                if write_native_primary_metadata:
                    _write_native_primary_metadata(
                        runtime_root=runtime_root,
                        spawn_id=primary_spawn_id,
                        spawn_dir=log_dir,
                        command=native_primary_metadata_command,
                        launch_cwd=launch_child_cwd,
                        launcher_pid=os.getpid(),
                        tui_pid=None,
                        activity="starting",
                        started_at_epoch=primary_started_epoch,
                        ended_at_epoch=None,
                        exit_code=None,
                        harness_session_id=resolved_harness_session_id,
                        runtime_metadata=native_primary_runtime_metadata,
                    )

                def _record_primary_started(child_pid: int) -> None:
                    nonlocal native_primary_tui_pid
                    native_primary_tui_pid = child_pid
                    lifecycle_service.mark_running(
                        primary_spawn_id,
                        launch_mode=FOREGROUND_LAUNCH_MODE,
                        worker_pid=child_pid,
                    )
                    assert managed.attempt is not None
                    managed.attempt.record_started(
                        runtime_context, str(primary_spawn_id), resolved_harness_session_id,
                    )
                    if write_native_primary_metadata:
                        _write_native_primary_metadata(
                            runtime_root=runtime_root,
                            spawn_id=primary_spawn_id,
                            spawn_dir=log_dir,
                            command=native_primary_metadata_command,
                            launch_cwd=launch_child_cwd,
                            launcher_pid=os.getpid(),
                            tui_pid=child_pid,
                            activity="idle",
                            started_at_epoch=primary_started_epoch,
                            ended_at_epoch=None,
                            exit_code=None,
                            harness_session_id=resolved_harness_session_id,
                            runtime_metadata=native_primary_runtime_metadata,
                        )

                (
                    exit_code,
                    managed_session_id,
                    managed_cancelled,
                ) = _execute_primary_process(
                    harness_id=harness_id,
                    primary_spawn_id=primary_spawn_id,
                    log_dir=log_dir,
                    control_root=control_root,
                    launch_cwd=launch_child_cwd,
                    task_cwd=task_cwd,
                    child_env=child_env,
                    launch_spec=launch_spec,
                    command=command,
                    harness_contract=harness_adapter.contract,
                    managed=managed,
                    runtime_root=runtime_root,
                    run_primary_process_with_capture_fn=run_primary_process_with_capture_fn,
                    run_primary_attach_fn=run_primary_attach_fn,
                    on_running=_record_primary_started,
                )
                if managed_session_id is not None:
                    resolved_harness_session_id = managed_session_id
                _persist_blackbox_output_artifact(
                    artifacts=artifacts,
                    spawn_id=primary_spawn_id,
                    log_dir=log_dir,
                )
                with suppress(Exception):
                    lifecycle_service.record_exited(
                        primary_spawn_id,
                        exit_code=exit_code,
                    )
            finally:
                try:
                    native_identity_error = None
                    if identity_plan is not None and primary_started_epoch > 0:
                        native_identity_error = harness_adapter.verify_native_identity(
                            identity_plan,
                        )
                        if native_identity_error:
                            logger.warning(
                                "Native identity verification conflict: %s", native_identity_error
                            )
                            exit_code = 1
                    (
                        exit_code,
                        resolved_harness_session_id,
                    ) = _finalize_lifecycle_and_observe_session(
                        primary_spawn_id=primary_spawn_id,
                        exit_code=exit_code,
                        resolved_harness_session_id=resolved_harness_session_id,
                        expected_harness_session_id=expected_harness_session_id,
                        harness_adapter=harness_adapter,
                        artifacts=artifacts,
                        project_root=control_root,
                        launch_child_cwd=launch_child_cwd,
                        model_id=session_metadata.model,
                        runtime_root=runtime_root,
                        primary_started=primary_started,
                        primary_started_epoch=primary_started_epoch,
                        primary_started_local_iso=primary_started_local_iso,
                        managed=managed,
                        spawn_service=spawn_service,
                        observe_adapter_session_id=not write_native_primary_metadata,
                        cancellation_observed=managed_cancelled,
                        native_identity_error=native_identity_error,
                    )
                    if write_native_primary_metadata and primary_spawn_id is not None:
                        observation = harness_adapter.observe_primary_session_id(
                            native_identity_plan=identity_plan,
                            command=command,
                            child_env=child_env,
                            launch_child_cwd=launch_child_cwd,
                            started_at_epoch=(
                                primary_started_epoch if primary_started_epoch > 0.0 else None
                            ),
                            expected_session_id=expected_harness_session_id,
                            requested_session_id=requested_harness_session_id,
                            resolved_session_id=resolved_harness_session_id,
                            exit_code=exit_code,
                        )
                        if observation.session_id:
                            resolved_harness_session_id = bind_harness_session_id(
                                runtime_root=runtime_root,
                                spawn_id=primary_spawn_id,
                                record_session_id=managed.record_harness_session_id,
                                session_id=observation.session_id,
                                source="observed",
                                current_session_id=resolved_harness_session_id,
                                chat_id=managed.chat_id,
                            )
                        _write_native_primary_metadata(
                            runtime_root=runtime_root,
                            spawn_id=primary_spawn_id,
                            spawn_dir=resolve_spawn_log_dir(
                                config_root, primary_spawn_id, runtime_root=runtime_root
                            ),
                            command=native_primary_metadata_command,
                            launch_cwd=launch_child_cwd,
                            launcher_pid=os.getpid(),
                            tui_pid=native_primary_tui_pid,
                            activity="finalizing",
                            started_at_epoch=(
                                primary_started_epoch if primary_started_epoch > 0 else None
                            ),
                            ended_at_epoch=time.time(),
                            exit_code=exit_code,
                            harness_session_id=resolved_harness_session_id,
                            runtime_metadata=native_primary_runtime_metadata,
                            harness_session_discovery=observation.discovery,
                            harness_session_discovery_detail=observation.detail,
                        )
                finally:
                    if primary_spawn_id is not None:
                        try:
                            harness_adapter.cleanup_prelaunch(
                                runtime_root=runtime_root,
                                spawn_id=primary_spawn_id,
                                chat_id=managed.chat_id,
                                state=prelaunch_state,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to clean up adapter prelaunch state for primary spawn",
                                exc_info=True,
                            )
    except FileNotFoundError:
        logger.debug("Harness command not found", exc_info=True)
        exit_code = 2

    return ProcessOutcome(
        command=command,
        exit_code=exit_code,
        chat_id=chat_id,
        primary_spawn_id=primary_spawn_id,
        primary_started=primary_started,
        primary_started_epoch=primary_started_epoch,
        primary_started_local_iso=primary_started_local_iso,
        resolved_harness_session_id=resolved_harness_session_id,
    )


__all__ = [
    "ProcessOutcome",
    "run_harness_process",
    "run_primary_attach",
    "run_primary_process_with_capture",
    "select_process_backend",
    "select_process_launcher",
]
