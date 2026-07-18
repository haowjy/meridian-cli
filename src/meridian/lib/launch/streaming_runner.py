"""Bidirectional spawn execution with lifecycle-owned terminal finalization."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import signal
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from meridian.lib.bootstrap.services import (
    build_spawn_application_service_from_roots,
    build_spawn_lifecycle_service_from_roots,
)
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.clock import Clock, RealClock
from meridian.lib.core.domain import Spawn, SpawnStatus
from meridian.lib.core.spawn_lifecycle import ExecutionTerminalFacts
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import StreamEvent
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.common import parse_json_stream_event, unwrap_event_payload
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.harness.extractor import StreamingExtractor
from meridian.lib.harness.semantics import (
    PrimaryEventScopeTracker,
    TerminalEventOutcome,
    terminal_outcome,
)
from meridian.lib.launch.constants import (
    CURSOR_INACTIVITY_TIMEOUT_SECONDS,
    DEFAULT_INFRA_EXIT_CODE,
    HISTORY_FILENAME,
    LAST_OBSERVED_EVENT_FILENAME,
    OUTPUT_FILENAME,
    REPORT_FILENAME,
    REPORT_WATCHDOG_GRACE_SECONDS,
    REPORT_WATCHDOG_POLL_SECONDS,
    RUNNER_LIFECYCLE_FILENAME,
    STDERR_FILENAME,
    SUBPROCESS_REPORT_WATCHDOG_POLL_SECONDS,
    TOKENS_FILENAME,
)
from meridian.lib.launch.context import LaunchContext
from meridian.lib.launch.env import (
    apply_pi_bind_time_env,
    resolve_pi_session_role,
    scope_pi_session_dir_for_spawn,
)
from meridian.lib.launch.errors import ErrorCategory, classify_error, should_retry
from meridian.lib.launch.extract import (
    FinalizeExtraction,
    enrich_finalize,
    reset_finalize_attempt_artifacts,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.launch.resolve import (
    resolve_pi_child_wave_timeout_seconds,
    resolve_pi_task_ping_interval_seconds,
    resolve_resident_deadline_seconds,
    resolve_resident_poll_seconds,
    resolve_startup_timeout_seconds,
)
from meridian.lib.launch.runner_helpers import (
    append_budget_exceeded_event as _append_budget_exceeded_event,
)
from meridian.lib.launch.runner_helpers import (
    append_text_to_stderr_artifact as _append_text_to_stderr_artifact,
)
from meridian.lib.launch.runner_helpers import (
    artifact_is_zero_bytes as _artifact_is_zero_bytes,
)
from meridian.lib.launch.runner_helpers import (
    guardrail_failure_text as _guardrail_failure_text,
)
from meridian.lib.launch.runner_helpers import (
    spawn_kind as _spawn_kind,
)
from meridian.lib.launch.runner_helpers import (
    write_structured_failure_artifact as _write_structured_failure_artifact,
)
from meridian.lib.launch.signals import signal_coordinator, signal_to_exit_code
from meridian.lib.launch.streaming.heartbeat import FileHeartbeat, HeartbeatTouch
from meridian.lib.launch.streaming.terminal_arbitrator import TriggerKind, arbitrate_terminal
from meridian.lib.safety.budget import Budget, BudgetBreach, LiveBudgetTracker
from meridian.lib.safety.guardrails import run_guardrails
from meridian.lib.safety.redaction import SecretSpec, redact_secret_bytes
from meridian.lib.state import paths as state_paths
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import ArtifactStore, make_artifact_key
from meridian.lib.state.atomic import append_text_line, atomic_write_bytes
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.spawn.model import (
    BACKGROUND_LAUNCH_MODE,
    FOREGROUND_LAUNCH_MODE,
    LaunchMode,
)
from meridian.lib.streaming.spawn_manager import DrainOutcome, SpawnManager
from meridian.lib.utils.time import minutes_to_seconds

if TYPE_CHECKING:
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.harness.connections.base import HarnessEvent
    from meridian.lib.state.spawn.model import CancelIntent

_DEFAULT_CONFIG = MeridianConfig()
DEFAULT_GUARDRAIL_TIMEOUT_SECONDS = _DEFAULT_CONFIG.guardrail_timeout_minutes * 60.0
logger = structlog.get_logger(__name__)
_HEARTBEAT_INTERVAL_SECS = 30.0


@dataclass(frozen=True)
class _AttemptRuntime:
    connection: HarnessConnection[Any] | None
    drain_exit_code: int
    drain_error: str | None
    timed_out: bool
    received_signal: signal.Signals | None
    budget_breach: BudgetBreach | None
    terminated_by_report_watchdog: bool
    terminated_by_inactivity: bool = False
    cancelled_by_request: bool = False
    terminal_observed: bool = False
    authoritative_terminal_status: SpawnStatus | None = None
    start_error: str | None = None


class StartupPhaseTimeout(TimeoutError):
    """The backend boot/connection/session-handshake phase exceeded its bound."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"startup phase timeout after {timeout_seconds:.3f}s")


@dataclass
class StreamingRunConclusion:
    """Accumulates execution outcome across retry attempts."""

    exit_code: int = DEFAULT_INFRA_EXIT_CODE
    failure_reason: str | None = None
    extracted: FinalizeExtraction | None = None
    final_attempt_terminal_observed: bool = False
    authoritative_terminal_status: SpawnStatus | None = None
    cancellation_observed: bool = False
    retries_attempted: int = 0

    def absorb_attempt(self, attempt: _AttemptRuntime) -> None:
        """Merge one attempt's terminal fields into the run conclusion."""

        self.exit_code = attempt.drain_exit_code
        self.final_attempt_terminal_observed = attempt.terminal_observed
        self.authoritative_terminal_status = attempt.authoritative_terminal_status
        self.cancellation_observed = self.cancellation_observed or attempt.cancelled_by_request

    def terminal_facts(
        self,
        *,
        received_signal: signal.Signals | None,
    ) -> ExecutionTerminalFacts:
        """Project accumulated runner evidence into lifecycle terminal facts."""

        cancellation_observed = (
            self.cancellation_observed
            or self.failure_reason in {"cancelled", "terminated"}
            or received_signal in {signal.SIGINT, signal.SIGTERM}
        )
        return ExecutionTerminalFacts(
            exit_code=self.exit_code,
            failure_reason=self.failure_reason,
            cancellation_observed=cancellation_observed,
            durable_report_completion=(
                self.extracted is not None and self.extracted.durable_report_completion
            ),
            terminal_status=self.authoritative_terminal_status,
        )


def _inactivity_terminal_outcome(
    extraction: FinalizeExtraction,
) -> tuple[int | None, str | None]:
    """Map inactivity termination to terminal exit_code/failure_reason updates.

    When a durable last-message report was recovered, treat the run as success.
    Otherwise finalize as ``stalled`` without rewriting exit_code (caller keeps
    the drain exit code from the inactivity stop).
    """

    if extraction.durable_report_completion:
        return 0, None
    return None, "stalled"


def _touch_heartbeat_file(
    runtime_root: Path,
    spawn_id: SpawnId,
    *,
    clock: Clock | None = None,
) -> None:
    FileHeartbeat(
        state_paths.heartbeat_path(runtime_root, spawn_id),
        clock=clock,
    ).touch()


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
    received_signal: list[signal.Signals | None],
    on_signal: Callable[[signal.Signals], None] | None = None,
) -> Callable[[], None] | None:
    """Install portable signal handlers that set the shutdown event.

    Uses signal.signal() instead of loop.add_signal_handler() for Windows
    compatibility (ProactorEventLoop does not support add_signal_handler).

    Returns a cleanup callable that restores previous handlers, or None if
    installation failed (non-main thread).
    """
    import threading

    if threading.current_thread() is not threading.main_thread():
        return None

    previous_handlers: dict[int, Any] = {}

    def _handle(signum: int, frame: object) -> None:
        if received_signal[0] is None:
            received_signal[0] = signal.Signals(signum)
            if on_signal is not None:
                with suppress(Exception):
                    on_signal(received_signal[0])
        loop.call_soon_threadsafe(shutdown_event.set)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[int(signum)] = signal.getsignal(signum)
            signal.signal(signum, _handle)
        except (ValueError, OSError):
            continue

    def _cleanup() -> None:
        for signum_int, prev in previous_handlers.items():
            with suppress(Exception):
                signal.signal(signal.Signals(signum_int), prev)

    return _cleanup


def _append_runner_lifecycle_event(
    path: Path,
    *,
    clock: Clock,
    event: str,
    phase: str,
    **details: object,
) -> None:
    """Best-effort append of runner-owned crash diagnostics."""

    payload = {
        "event": event,
        "timestamp": clock.utc_now_iso(),
        "pid": os.getpid(),
        "phase": phase,
        **details,
    }
    try:
        append_text_line(
            path,
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        )
    except Exception:
        logger.warning("Failed to append runner lifecycle evidence.", exc_info=True)


_ATTEMPT_STORE_ARTIFACTS = (
    HISTORY_FILENAME,
    OUTPUT_FILENAME,
    STDERR_FILENAME,
    TOKENS_FILENAME,
    REPORT_FILENAME,
)
_ATTEMPT_DISK_ARTIFACTS = (
    HISTORY_FILENAME,
    LAST_OBSERVED_EVENT_FILENAME,
    RUNNER_LIFECYCLE_FILENAME,
    STDERR_FILENAME,
    TOKENS_FILENAME,
    REPORT_FILENAME,
)


def _recover_interrupted_attempt_rotation(log_dir: Path, attempt_prefix: str) -> bool:
    """Fold or discard a leftover staging dir from a crashed preservation.

    Returns whether ``attempt_prefix/`` already exists after recovery.
    """

    staging_dir = log_dir / f"{attempt_prefix}.tmp"
    attempt_dir = log_dir / attempt_prefix
    if not staging_dir.is_dir():
        return attempt_dir.is_dir()
    if attempt_dir.exists():
        shutil.rmtree(staging_dir)
        return True
    os.replace(staging_dir, attempt_dir)
    return True


def _preserve_attempt_artifacts(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    log_dir: Path,
    completed_attempt: int,
) -> None:
    """Atomically move completed-attempt evidence out of the live artifact names.

    Commit point is ``os.replace(staging_dir, attempt_dir)``. A leftover
    ``attempt-N.tmp/`` from a crashed run is folded into ``attempt-N/`` when that
    directory is absent; otherwise the staging dir is discarded. Artifact-store
    copies and active-key deletion happen only after the filesystem commit so
    retries never read stale attempt-scoped store keys.
    """

    attempt_prefix = f"attempt-{completed_attempt}"
    staging_dir = log_dir / f"{attempt_prefix}.tmp"
    attempt_dir = log_dir / attempt_prefix
    already_committed = _recover_interrupted_attempt_rotation(log_dir, attempt_prefix)

    if already_committed:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        for name in _ATTEMPT_DISK_ARTIFACTS:
            target = log_dir / name
            if target.exists():
                os.replace(target, attempt_dir / name)
    else:
        staging_dir.mkdir(parents=True, exist_ok=True)
        for name in _ATTEMPT_DISK_ARTIFACTS:
            target = log_dir / name
            if target.exists():
                os.replace(target, staging_dir / name)
        os.replace(staging_dir, attempt_dir)

    for name in _ATTEMPT_STORE_ARTIFACTS:
        active_key = make_artifact_key(spawn_id, name)
        if artifacts.exists(active_key):
            artifacts.put(
                make_artifact_key(spawn_id, f"{attempt_prefix}/{name}"),
                artifacts.get(active_key),
            )

    for name in _ATTEMPT_STORE_ARTIFACTS:
        artifacts.delete(make_artifact_key(spawn_id, name))


def _scope_pi_session_dir_for_spawn(
    *,
    child_env: dict[str, str],
    spawn_id: SpawnId,
) -> None:
    """Scope Pi session storage to one launch to avoid stale fallback collisions."""

    scope_pi_session_dir_for_spawn(child_env=child_env, spawn_id=spawn_id)


def _persist_attempt_artifacts(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    log_dir: Path,
    secrets: tuple[SecretSpec, ...],
) -> None:
    for name in (
        HISTORY_FILENAME,
        STDERR_FILENAME,
        TOKENS_FILENAME,
    ):
        source = log_dir / name
        if not source.exists():
            continue
        payload = source.read_bytes()
        if name in {HISTORY_FILENAME, STDERR_FILENAME}:
            payload = redact_secret_bytes(payload, secrets)
        artifacts.put(make_artifact_key(spawn_id, name), payload)


def _retry_blocked_after_pi_child_started(
    *, harness_id: HarnessId, runtime_root: Path, current_spawn_id: SpawnId
) -> bool:
    """Return whether retrying would orphan already-started Pi child spawn work."""

    if harness_id is not HarnessId.PI:
        return False
    spawns_dir = runtime_root / "spawns"
    if not spawns_dir.is_dir():
        return False
    for child in spawns_dir.iterdir():
        if (
            child.name.startswith(".")
            or not spawn_store.is_spawn_id_shape(child.name)
            or not child.is_dir()
        ):
            continue
        state_path = child / "state.json"
        if not state_path.is_file():
            continue
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        state = cast("dict[str, object]", data)
        if state.get("parent_id") == str(current_spawn_id):
            return True
    return False


def _read_cancel_intent(runtime_root: Path, spawn_id: SpawnId) -> CancelIntent | None:
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    return None if record is None else record.cancel_intent


def _apply_cancel_intent_to_conclusion(
    conclusion: StreamingRunConclusion,
    *,
    runtime_root: Path,
    spawn_id: SpawnId,
) -> bool:
    intent = _read_cancel_intent(runtime_root, spawn_id)
    if intent is None:
        return False
    conclusion.exit_code = intent.exit_code
    conclusion.failure_reason = intent.error or "cancelled"
    conclusion.cancellation_observed = True
    return True


async def _sleep_retry_backoff_or_cancel(
    *,
    delay_seconds: float,
    shutdown_event: asyncio.Event,
    runtime_root: Path,
    spawn_id: SpawnId,
) -> bool:
    deadline = asyncio.get_running_loop().time() + max(0.0, delay_seconds)
    while True:
        if shutdown_event.is_set() or _read_cancel_intent(runtime_root, spawn_id) is not None:
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.1, remaining))


def _line_from_harness_event(event: HarnessEvent) -> str:
    if event.raw_text is not None and event.raw_text.strip():
        return event.raw_text
    payload: dict[str, object] = dict(event.payload)
    payload.setdefault("event", event.event_type)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _observe_budget_from_event(
    *,
    budget_tracker: LiveBudgetTracker | None,
    event: HarnessEvent,
) -> BudgetBreach | None:
    if budget_tracker is None:
        return None

    payload = unwrap_event_payload(event.payload)
    try:
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return budget_tracker.observe_json_line(encoded)


def _emit_stream_event(
    *,
    line: str,
    event_observer: Callable[[StreamEvent], None] | None,
    stream_stdout_to_terminal: bool,
) -> None:
    parsed = parse_json_stream_event(line)
    if parsed is None:
        return

    if event_observer is not None:
        try:
            event_observer(parsed)
        except Exception:
            logger.warning("Stream event observer failed.", exc_info=True)

    if not stream_stdout_to_terminal:
        return

    rendered = parsed.text.strip() if parsed.text is not None else parsed.raw_line.strip()
    if not rendered:
        return
    sys.stderr.write(f"{rendered}\n")
    sys.stderr.flush()


async def _consume_subscriber_events(
    *,
    subscriber: asyncio.Queue[HarnessEvent | None],
    budget_tracker: LiveBudgetTracker | None,
    budget_signal: asyncio.Event,
    budget_breach_holder: list[BudgetBreach | None],
    event_observer: Callable[[StreamEvent], None] | None,
    stream_stdout_to_terminal: bool,
    terminal_event_future: asyncio.Future[TerminalEventOutcome] | None = None,
    primary_scope_tracker: PrimaryEventScopeTracker | None = None,
    last_event_at: list[float] | None = None,
) -> None:
    while True:
        event = await subscriber.get()
        if event is None:
            return

        if last_event_at is not None:
            last_event_at[0] = asyncio.get_running_loop().time()

        if budget_breach_holder[0] is None:
            breach = _observe_budget_from_event(
                budget_tracker=budget_tracker,
                event=event,
            )
            if breach is not None:
                budget_breach_holder[0] = breach
                budget_signal.set()

        if terminal_event_future is not None and not terminal_event_future.done():
            if primary_scope_tracker is not None:
                event_outcome = primary_scope_tracker.terminal_outcome(event)
            else:
                event_outcome = terminal_outcome(event)
            if event_outcome is not None:
                terminal_event_future.set_result(event_outcome)

        if event_observer is not None or stream_stdout_to_terminal:
            line = _line_from_harness_event(event)
            _emit_stream_event(
                line=line,
                event_observer=event_observer,
                stream_stdout_to_terminal=stream_stdout_to_terminal,
            )


async def _report_watchdog(
    *,
    report_path: Path,
    completion_event: asyncio.Event,
    manager: SpawnManager,
    spawn_id: SpawnId,
    grace_seconds: float = REPORT_WATCHDOG_GRACE_SECONDS,
) -> bool:
    while not report_path.exists():
        if completion_event.is_set():
            return False
        await asyncio.sleep(REPORT_WATCHDOG_POLL_SECONDS)

    deadline = asyncio.get_running_loop().time() + grace_seconds
    while asyncio.get_running_loop().time() < deadline:
        if completion_event.is_set():
            return False
        await asyncio.sleep(REPORT_WATCHDOG_POLL_SECONDS)

    if completion_event.is_set():
        return False

    await manager.stop_spawn(spawn_id, status="cancelled", exit_code=1, error="report_watchdog")
    logger.info(
        "Report watchdog stopped active streaming connection after grace timeout.",
        spawn_id=str(spawn_id),
        grace_seconds=grace_seconds,
    )
    return True


async def _inactivity_watchdog(
    *,
    last_event_at: list[float],
    completion_event: asyncio.Event,
    manager: SpawnManager,
    spawn_id: SpawnId,
    timeout_seconds: float,
    poll_seconds: float = SUBPROCESS_REPORT_WATCHDOG_POLL_SECONDS,
) -> bool:
    loop = asyncio.get_running_loop()
    while True:
        if completion_event.is_set():
            return False
        idle = loop.time() - last_event_at[0]
        if idle >= timeout_seconds:
            break
        await asyncio.sleep(min(poll_seconds, max(0.0, timeout_seconds - idle)))
    if completion_event.is_set():
        return False
    await manager.stop_spawn(spawn_id, status="failed", exit_code=1, error="inactivity_stall")
    logger.info(
        "Inactivity watchdog stopped stalled spawn after silence.",
        spawn_id=str(spawn_id),
        timeout_seconds=timeout_seconds,
    )
    return True


async def _start_spawn_with_timeout(
    *,
    manager: SpawnManager,
    config: ConnectionConfig,
    run_spec: ResolvedLaunchSpec,
    timeout_seconds: float,
) -> HarnessConnection[Any]:
    """Start a managed connection within the shared startup-phase bound."""

    try:
        async with asyncio.timeout(timeout_seconds):
            return await manager.start_spawn(config, run_spec)
    except TimeoutError as exc:
        raise StartupPhaseTimeout(timeout_seconds) from exc


async def run_streaming_spawn(
    *,
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
    runtime_root: Path,
    project_root: Path,
    spawn_id: SpawnId,
    startup_timeout_seconds: float,
    stream_to_terminal: bool = False,
    heartbeat_touch: HeartbeatTouch | None = None,
    heartbeat_interval_secs: float = _HEARTBEAT_INTERVAL_SECS,
    lifecycle_service: SpawnLifecycleService | None = None,
    on_control_endpoint_ready: Callable[[str], None] | None = None,
) -> DrainOutcome:
    """Run one streaming spawn to completion without spawn-store finalization.

    Callers are responsible for resolving *spec* via ``build_launch_context()``
    before calling this function.  I-8 (executors stay mechanism-only): this
    executor accepts a fully-composed spec and MUST NOT perform composition.
    """

    resolved_heartbeat_touch = heartbeat_touch or (
        lambda: _touch_heartbeat_file(runtime_root, spawn_id)
    )
    manager = SpawnManager(
        runtime_root=runtime_root,
        project_root=project_root,
        heartbeat_interval_secs=heartbeat_interval_secs,
        heartbeat_touch=lambda _runtime_root, _spawn_id: resolved_heartbeat_touch(),
    )

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    received_signal: list[signal.Signals | None] = [None]
    signal_cleanup = _install_signal_handlers(loop, shutdown_event, received_signal)

    completion_task: asyncio.Task[DrainOutcome | None] | None = None
    signal_task: asyncio.Task[bool] | None = None
    consume_task: asyncio.Task[None] | None = None
    terminal_event_future: asyncio.Future[TerminalEventOutcome] | None = None
    terminal_outcome: TerminalEventOutcome | None = None
    subscriber: asyncio.Queue[HarnessEvent | None] | None = None
    run_spec = spec
    spawn_store.update_spawn(
        runtime_root,
        spawn_id,
        runner_pid=os.getpid(),
    )
    resolved_lifecycle = lifecycle_service or build_spawn_lifecycle_service_from_roots(
        project_root,
        runtime_root,
    )
    try:
        connection = await _start_spawn_with_timeout(
            manager=manager,
            config=config,
            run_spec=run_spec,
            timeout_seconds=startup_timeout_seconds,
        )
        if on_control_endpoint_ready is not None:
            endpoint = manager.control_endpoint(spawn_id)
            if endpoint is not None:
                try:
                    on_control_endpoint_ready(endpoint)
                except Exception:
                    logger.warning(
                        "Control endpoint callback failed.",
                        spawn_id=str(spawn_id),
                        exc_info=True,
                    )
        await manager.start_heartbeat(spawn_id)
        subscriber = manager.subscribe(spawn_id)
        if subscriber is None:
            raise RuntimeError("failed to subscribe to spawn stream")

        terminal_event_future = loop.create_future()
        terminal_event_capture = (
            terminal_event_future
            if manager.raw_terminal_frames_are_authoritative(spawn_id)
            else None
        )
        primary_scope_tracker = (
            PrimaryEventScopeTracker(
                primary_event_scope=connection.primary_event_scope
            )
            if terminal_event_capture is not None
            else None
        )
        completion_task = asyncio.create_task(manager.wait_for_completion(spawn_id))
        consume_task = asyncio.create_task(
            _consume_subscriber_events(
                subscriber=subscriber,
                budget_tracker=None,
                budget_signal=asyncio.Event(),
                budget_breach_holder=[None],
                event_observer=None,
                stream_stdout_to_terminal=stream_to_terminal,
                terminal_event_future=terminal_event_capture,
                primary_scope_tracker=primary_scope_tracker,
            )
        )
        signal_task = asyncio.create_task(shutdown_event.wait())

        decision = await arbitrate_terminal(
            completion_task=completion_task,
            terminal_event_future=terminal_event_future,
            signal_task=signal_task,
        )
        terminal_outcome = decision.terminal_outcome
        if decision.stop_required:
            stop_exit_code = decision.synthetic_exit_code
            if decision.trigger == TriggerKind.SIGNAL:
                stop_exit_code = signal_to_exit_code(received_signal[0]) or 130
            if stop_exit_code is None:
                raise RuntimeError("terminal decision requires an exit code")
            await manager.stop_spawn(
                spawn_id,
                status=decision.synthetic_status or "cancelled",
                exit_code=stop_exit_code,
                error=decision.synthetic_error,
            )

        outcome = await completion_task
        if outcome is None:
            raise RuntimeError("streaming spawn completed without drain outcome")
        if terminal_outcome is not None:
            resolved_outcome = DrainOutcome(
                status=terminal_outcome.status,
                exit_code=terminal_outcome.exit_code,
                error=terminal_outcome.error,
                duration_secs=outcome.duration_secs,
            )
        else:
            resolved_outcome = outcome
        with suppress(Exception):
            resolved_lifecycle.record_exited(
                str(spawn_id),
                exit_code=resolved_outcome.exit_code,
            )
        return resolved_outcome
    finally:
        with signal_coordinator().mask_sigterm():
            if subscriber is not None:
                manager.unsubscribe(spawn_id)
            for task in (completion_task, signal_task, consume_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            if signal_cleanup is not None:
                signal_cleanup()
            with suppress(Exception):
                await manager.shutdown(status="cancelled", exit_code=1, error="shutdown")


async def _run_streaming_attempt(
    *,
    run: Spawn,
    runtime_root: Path,
    launch_mode: LaunchMode,
    log_dir: Path,
    manager: SpawnManager,
    config: ConnectionConfig,
    run_spec: ResolvedLaunchSpec,
    budget_tracker: LiveBudgetTracker | None,
    signal_event: asyncio.Event,
    received_signal: list[signal.Signals | None],
    timeout_seconds: float | None,
    startup_timeout_seconds: float = 300.0,
    event_observer: Callable[[StreamEvent], None] | None,
    stream_stdout_to_terminal: bool,
    lifecycle_service: SpawnLifecycleService,
    runner_phase: list[str] | None = None,
) -> _AttemptRuntime:
    completion_task: asyncio.Task[DrainOutcome | None] | None = None
    timeout_task: asyncio.Task[None] | None = None
    signal_task: asyncio.Task[bool] | None = None
    budget_task: asyncio.Task[bool] | None = None
    watchdog_task: asyncio.Task[bool] | None = None
    inactivity_task: asyncio.Task[bool] | None = None
    consume_task: asyncio.Task[None] | None = None
    completion_event = asyncio.Event()
    budget_signal = asyncio.Event()
    budget_breach_holder: list[BudgetBreach | None] = [None]
    last_event_at: list[float] = [asyncio.get_running_loop().time()]
    terminal_event_future: asyncio.Future[TerminalEventOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    terminal_event_capture: asyncio.Future[TerminalEventOutcome] | None = None
    primary_scope_tracker: PrimaryEventScopeTracker | None = None
    subscriber: asyncio.Queue[HarnessEvent | None] | None = None
    connection: HarnessConnection[Any] | None = None
    drain_exit_code = DEFAULT_INFRA_EXIT_CODE
    drain_error: str | None = None
    timed_out = False
    terminated_by_report_watchdog = False
    terminated_by_inactivity = False
    cancelled_by_request = False
    terminal_outcome: TerminalEventOutcome | None = None
    authoritative_terminal_status: SpawnStatus | None = None
    try:
        if runner_phase is not None:
            runner_phase[0] = "starting_harness"
        connection = await _start_spawn_with_timeout(
            manager=manager,
            config=config,
            run_spec=run_spec,
            timeout_seconds=startup_timeout_seconds,
        )
        terminal_event_capture = (
            terminal_event_future
            if manager.raw_terminal_frames_are_authoritative(run.spawn_id)
            else None
        )
        primary_scope_tracker = (
            PrimaryEventScopeTracker(
                primary_event_scope=connection.primary_event_scope
            )
            if terminal_event_capture is not None
            else None
        )
        await manager.start_heartbeat(run.spawn_id)
        lifecycle_service.mark_running(
            run.spawn_id,
            launch_mode=launch_mode,
            worker_pid=connection.subprocess_pid,
        )
        subscriber = manager.subscribe(run.spawn_id)
        if subscriber is None:
            raise RuntimeError("failed to subscribe to spawn stream")

        if runner_phase is not None:
            runner_phase[0] = "consuming_events"
        completion_task = asyncio.create_task(manager.wait_for_completion(run.spawn_id))
        completion_task.add_done_callback(lambda _: completion_event.set())
        consume_task = asyncio.create_task(
            _consume_subscriber_events(
                subscriber=subscriber,
                budget_tracker=budget_tracker,
                budget_signal=budget_signal,
                budget_breach_holder=budget_breach_holder,
                event_observer=event_observer,
                stream_stdout_to_terminal=stream_stdout_to_terminal,
                terminal_event_future=terminal_event_capture,
                primary_scope_tracker=primary_scope_tracker,
                last_event_at=last_event_at,
            )
        )
        signal_task = asyncio.create_task(signal_event.wait())
        if budget_tracker is not None:
            budget_task = asyncio.create_task(budget_signal.wait())
        if timeout_seconds is not None and timeout_seconds > 0:
            timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
        watchdog_task = asyncio.create_task(
            _report_watchdog(
                report_path=log_dir / REPORT_FILENAME,
                completion_event=completion_event,
                manager=manager,
                spawn_id=run.spawn_id,
            )
        )
        if config.harness_id == HarnessId.CURSOR:
            inactivity_task = asyncio.create_task(
                _inactivity_watchdog(
                    last_event_at=last_event_at,
                    completion_event=completion_event,
                    manager=manager,
                    spawn_id=run.spawn_id,
                    timeout_seconds=CURSOR_INACTIVITY_TIMEOUT_SECONDS,
                )
            )

        decision = await arbitrate_terminal(
            completion_task=completion_task,
            terminal_event_future=terminal_event_future,
            signal_task=signal_task,
            timeout_task=timeout_task,
            budget_task=budget_task,
            watchdog_task=watchdog_task,
            inactivity_task=inactivity_task,
        )
        terminal_outcome = decision.terminal_outcome
        if decision.trigger == TriggerKind.BUDGET:
            await manager.stop_spawn(
                run.spawn_id,
                status="failed",
                exit_code=DEFAULT_INFRA_EXIT_CODE,
                error="budget_exceeded",
            )
            drain_exit_code = DEFAULT_INFRA_EXIT_CODE
        elif decision.trigger == TriggerKind.TIMEOUT:
            timed_out = True
            await manager.stop_spawn(
                run.spawn_id,
                status="timed_out",
                exit_code=3,
                error="timeout",
            )
            drain_exit_code = 3
        elif decision.trigger == TriggerKind.WATCHDOG:
            terminated_by_report_watchdog = not decision.watchdog_noop
        elif decision.trigger == TriggerKind.INACTIVITY:
            terminated_by_inactivity = not decision.watchdog_noop
        elif decision.stop_required:
            stop_exit_code = decision.synthetic_exit_code
            if decision.trigger == TriggerKind.SIGNAL:
                cancelled_by_request = True
                stop_exit_code = signal_to_exit_code(received_signal[0]) or 130
            if stop_exit_code is None:
                raise RuntimeError("terminal decision requires an exit code")
            await manager.stop_spawn(
                run.spawn_id,
                status=decision.synthetic_status or "cancelled",
                exit_code=stop_exit_code,
                error=decision.synthetic_error,
            )
            drain_exit_code = stop_exit_code
            drain_error = decision.synthetic_error

        drain_outcome = await completion_task
        if drain_outcome is not None and terminal_outcome is None:
            if timed_out and drain_outcome.status != "succeeded":
                authoritative_terminal_status = "timed_out"
                drain_exit_code = 3
                drain_error = "timeout"
            else:
                drain_exit_code = drain_outcome.exit_code
                drain_error = drain_outcome.error
                if drain_outcome.authoritative and drain_outcome.status != "succeeded":
                    authoritative_terminal_status = drain_outcome.status
            if timed_out and drain_outcome.status == "succeeded":
                timed_out = False
            if drain_outcome.error == "report_watchdog":
                terminated_by_report_watchdog = True
            if drain_outcome.error == "inactivity_stall":
                terminated_by_inactivity = True

        # The watchdog resolves the completion future mid-flight inside
        # stop_spawn(), so completion_task can finish before watchdog_task.
        # Give the watchdog a brief window to land and reconcile the flag.
        if not terminated_by_report_watchdog:
            if watchdog_task.done():
                with suppress(Exception):
                    terminated_by_report_watchdog = bool(watchdog_task.result())
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(watchdog_task), timeout=2.0)
                    terminated_by_report_watchdog = bool(watchdog_task.result())
                except (TimeoutError, asyncio.CancelledError):
                    pass
        with suppress(Exception):
            lifecycle_service.record_exited(
                str(run.spawn_id),
                exit_code=drain_exit_code,
            )
    except Exception as exc:
        return _AttemptRuntime(
            connection=connection,
            drain_exit_code=DEFAULT_INFRA_EXIT_CODE,
            drain_error=str(exc),
            timed_out=False,
            received_signal=received_signal[0],
            budget_breach=budget_breach_holder[0],
            terminated_by_report_watchdog=terminated_by_report_watchdog,
            terminated_by_inactivity=terminated_by_inactivity,
            cancelled_by_request=cancelled_by_request,
            terminal_observed=False,
            authoritative_terminal_status=None,
            start_error=str(exc),
        )
    finally:
        if subscriber is not None:
            manager.unsubscribe(run.spawn_id)
        for task in (
            timeout_task,
            signal_task,
            budget_task,
            watchdog_task,
            inactivity_task,
            consume_task,
        ):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if manager.get_connection(run.spawn_id) is not None:
            with suppress(Exception):
                await manager.stop_spawn(run.spawn_id)

    pi_drain_terminal = config.harness_id == HarnessId.PI and drain_error is not None
    return _AttemptRuntime(
        connection=connection,
        drain_exit_code=drain_exit_code,
        drain_error=drain_error,
        timed_out=timed_out,
        received_signal=received_signal[0],
        budget_breach=budget_breach_holder[0],
        terminated_by_report_watchdog=terminated_by_report_watchdog,
        terminated_by_inactivity=terminated_by_inactivity,
        cancelled_by_request=cancelled_by_request,
        terminal_observed=(
            terminal_outcome is not None
            or pi_drain_terminal
            or authoritative_terminal_status is not None
        ),
        authoritative_terminal_status=authoritative_terminal_status,
    )


async def execute_with_streaming(
    run: Spawn,
    *,
    request: SpawnRequest,
    launch_context: LaunchContext,
    project_root: Path,
    runtime_root: Path,
    artifacts: ArtifactStore,
    budget: Budget | None = None,
    space_spent_usd: float = 0.0,
    guardrails: tuple[Path, ...] = (),
    guardrail_timeout_seconds: float = DEFAULT_GUARDRAIL_TIMEOUT_SECONDS,
    secrets: tuple[SecretSpec, ...] = (),
    harness_session_id_observer: Callable[[str], None] | None = None,
    event_observer: Callable[[StreamEvent], None] | None = None,
    stream_stdout_to_terminal: bool = False,
    stream_stderr_to_terminal: bool = False,
    debug: bool = False,
    clock: Clock | None = None,
    heartbeat_touch: HeartbeatTouch | None = None,
    heartbeat_interval_secs: float = _HEARTBEAT_INTERVAL_SECS,
) -> int:
    """Execute one streaming spawn and always finalize the spawn row.

    I-8 ownership: composition happens in driving adapters. This executor
    consumes pre-composed `LaunchContext` and does subprocess/transport
    mechanics only.
    """

    _ = stream_stderr_to_terminal
    resolved_clock = clock or RealClock()
    started_at = resolved_clock.monotonic()
    started_at_epoch = resolved_clock.time()
    resolved_heartbeat_touch = heartbeat_touch or (
        lambda: _touch_heartbeat_file(
            runtime_root,
            run.spawn_id,
        )
    )
    conclusion = StreamingRunConclusion()
    lifecycle_service: SpawnLifecycleService | None = None
    manager: SpawnManager | None = None
    signal_cleanup: Callable[[], None] | None = None
    loop: asyncio.AbstractEventLoop | None = None
    received_signal: list[signal.Signals | None] = [None]
    runner_phase = ["setup"]
    lifecycle_path: Path | None = None
    lifecycle_active = [False]
    atexit_callback: Callable[[], None] | None = None

    try:
        log_dir = resolve_spawn_log_dir(
            project_root, run.spawn_id, runtime_root=runtime_root
        )
        lifecycle_path = log_dir / RUNNER_LIFECYCLE_FILENAME
        output_log_path = log_dir / HISTORY_FILENAME
        report_path = log_dir / REPORT_FILENAME

        def _record_lifecycle(event: str, **details: object) -> None:
            assert lifecycle_path is not None
            _append_runner_lifecycle_event(
                lifecycle_path,
                clock=resolved_clock,
                event=event,
                phase=runner_phase[0],
                **details,
            )

        def _record_atexit() -> None:
            if lifecycle_active[0]:
                _record_lifecycle("atexit")

        atexit_callback = _record_atexit
        lifecycle_active[0] = True
        atexit.register(atexit_callback)
        _record_lifecycle("runner_started")

        timeout_seconds = minutes_to_seconds(request.execution_policy.timeout)
        startup_timeout_seconds = resolve_startup_timeout_seconds(
            config_snapshot=launch_context.runtime.config_snapshot,
        )
        pi_child_wave_timeout_seconds = resolve_pi_child_wave_timeout_seconds(
            explicit_timeout_seconds=None,
            config_snapshot=launch_context.runtime.config_snapshot,
        )
        resident_deadline_seconds = resolve_resident_deadline_seconds(
            config_snapshot=launch_context.runtime.config_snapshot,
        )
        resident_poll_seconds = resolve_resident_poll_seconds(
            config_snapshot=launch_context.runtime.config_snapshot,
        )
        pi_task_ping_interval_seconds = resolve_pi_task_ping_interval_seconds(
            explicit_interval_seconds=request.pi_task_ping_interval_seconds,
            config_snapshot=launch_context.runtime.config_snapshot,
        )
        max_retries = max(request.retry.max_attempts - 1, 0)
        retry_backoff_seconds = request.retry.backoff_secs

        resolved_harness_id = launch_context.harness.id
        child_cwd = launch_context.binding.child_cwd
        control_root = launch_context.control_root
        spec = launch_context.binding.spec
        child_env = dict(launch_context.binding.environment.final_env)
        harness = launch_context.harness
        harness_bundle = get_harness_bundle(resolved_harness_id)
        pi_session_role = (
            resolve_pi_session_role(interactive=launch_context.binding.run_params.interactive)
            if resolved_harness_id is HarnessId.PI
            else None
        )
        if resolved_harness_id is HarnessId.PI and pi_session_role == "spawned":
            _scope_pi_session_dir_for_spawn(
                child_env=child_env,
                spawn_id=run.spawn_id,
            )
        if resolved_harness_id is HarnessId.PI:
            assert pi_session_role is not None
            apply_pi_bind_time_env(
                child_env,
                launch_role=pi_session_role,
                timeout_seconds=pi_child_wave_timeout_seconds,
                interval_seconds=pi_task_ping_interval_seconds,
                reset_on_activity=request.pi_task_ping_reset_on_activity,
            )

        spawn_store.update_spawn(
            runtime_root,
            run.spawn_id,
            control_root=control_root.as_posix(),
            task_cwd=(
                launch_context.task_cwd.as_posix()
                if (
                    launch_context.task_cwd is not None
                    and launch_context.task_cwd.resolve() != control_root.resolve()
                )
                else None
            ),
            execution_cwd=str(child_cwd),
        )

        tracer: DebugTracer | None = None
        if debug:
            from meridian.lib.observability.debug_tracer import DebugTracer

            tracer = DebugTracer(
                spawn_id=str(run.spawn_id),
                debug_path=log_dir / "debug.jsonl",
                echo_stderr=stream_stdout_to_terminal,
            )

        observed_harness_session_id: str | None = None

        def _record_harness_session_id(session_id: str) -> None:
            nonlocal observed_harness_session_id
            normalized = session_id.strip()
            if not normalized or normalized == observed_harness_session_id:
                return
            spawn_store.update_spawn(
                runtime_root,
                run.spawn_id,
                harness_session_id=normalized,
            )
            observed_harness_session_id = normalized
            if harness_session_id_observer is not None:
                harness_session_id_observer(normalized)

        config = ConnectionConfig(
            spawn_id=run.spawn_id,
            harness_id=resolved_harness_id,
            prompt=spec.prompt,
            control_root=control_root,
            child_env=child_env,
            runtime_root=runtime_root,
            task_cwd=child_cwd if child_cwd.resolve() != control_root.resolve() else None,
            system=getattr(spec, "appended_system_prompt", None),
            timeout_seconds=timeout_seconds,
            pi_child_wave_timeout_seconds=pi_child_wave_timeout_seconds,
            resident_deadline_seconds=resident_deadline_seconds,
            resident_poll_seconds=resident_poll_seconds,
            resident_rearm_budget=request.execution_policy.resident_rearm_budget,
            pi_task_ping_interval_seconds=pi_task_ping_interval_seconds,
            pi_task_ping_reset_on_activity=request.pi_task_ping_reset_on_activity,
            pi_session_role=pi_session_role,
            debug_tracer=tracer,
            session_id_observer=_record_harness_session_id,
        )

        # I-10: spawn row MUST exist before execute_with_streaming is called.
        # Mid-flight row creation is forbidden — callers must call start_spawn first.
        spawn_row = spawn_store.get_spawn(runtime_root, run.spawn_id)
        if spawn_row is None:
            raise RuntimeError(
                f"execute_with_streaming precondition violated: "
                f"no spawn row exists for {run.spawn_id!r}. "
                "Call start_spawn() before execute_with_streaming()."
            )
        spawn_store.update_spawn(
            runtime_root,
            run.spawn_id,
            runner_pid=os.getpid(),
        )
        resolved_launch_mode: LaunchMode = (
            BACKGROUND_LAUNCH_MODE
            if spawn_row.launch_mode == BACKGROUND_LAUNCH_MODE
            else FOREGROUND_LAUNCH_MODE
        )

        materialized_session_id = (spec.continue_session_id or "").strip()
        if not materialized_session_id:
            seeded_session_id = harness.derive_streaming_seeded_session_id(spec=spec)
            if seeded_session_id:
                _record_harness_session_id(seeded_session_id)
        if materialized_session_id and materialized_session_id != (
            request.session.requested_harness_session_id or ""
        ):
            _record_harness_session_id(materialized_session_id)

        budget_tracker = (
            LiveBudgetTracker(budget=budget, space_spent_usd=space_spent_usd)
            if budget is not None
            else None
        )
        preflight_breach = budget_tracker.check() if budget_tracker is not None else None
        manager = SpawnManager(
            runtime_root=runtime_root,
            project_root=project_root,
            heartbeat_interval_secs=heartbeat_interval_secs,
            heartbeat_touch=lambda _runtime_root, _spawn_id: resolved_heartbeat_touch(),
        )
        lifecycle_service = build_spawn_lifecycle_service_from_roots(
            project_root,
            runtime_root,
        )

        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        signal_cleanup = _install_signal_handlers(
            loop,
            shutdown_event,
            received_signal,
            on_signal=lambda received: _record_lifecycle(
                "signal_received",
                signal=received.name,
                signal_number=received.value,
            ),
        )

        try:
            while True:
                if _apply_cancel_intent_to_conclusion(
                    conclusion,
                    runtime_root=runtime_root,
                    spawn_id=run.spawn_id,
                ):
                    break

                attempt_number = conclusion.retries_attempted + 1
                if attempt_number > 1:
                    _preserve_attempt_artifacts(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        log_dir=log_dir,
                        completed_attempt=attempt_number - 1,
                    )
                runner_phase[0] = "starting_attempt"
                _record_lifecycle("attempt_started", attempt=attempt_number)
                reset_finalize_attempt_artifacts(
                    artifacts=artifacts,
                    spawn_id=run.spawn_id,
                    log_dir=log_dir,
                )

                if preflight_breach is not None:
                    conclusion.exit_code = DEFAULT_INFRA_EXIT_CODE
                    conclusion.failure_reason = "budget_exceeded"
                    _append_budget_exceeded_event(run=run, breach=preflight_breach)
                    break

                attempt = await _run_streaming_attempt(
                    run=run,
                    runtime_root=runtime_root,
                    launch_mode=resolved_launch_mode,
                    log_dir=log_dir,
                    manager=manager,
                    config=config,
                    run_spec=spec,
                    budget_tracker=budget_tracker,
                    signal_event=shutdown_event,
                    received_signal=received_signal,
                    timeout_seconds=timeout_seconds,
                    startup_timeout_seconds=startup_timeout_seconds,
                    event_observer=event_observer,
                    stream_stdout_to_terminal=stream_stdout_to_terminal,
                    lifecycle_service=lifecycle_service,
                    runner_phase=runner_phase,
                )
                runner_phase[0] = "processing_attempt"
                conclusion.absorb_attempt(attempt)
                if attempt.start_error is not None:
                    logger.info(
                        "Failed to execute streaming spawn attempt.",
                        spawn_id=str(run.spawn_id),
                        harness_id=str(harness.id),
                        error=attempt.start_error,
                    )
                    conclusion.failure_reason = attempt.start_error
                    _append_text_to_stderr_artifact(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        text=attempt.start_error,
                        secrets=secrets,
                    )
                attempt_cancelled = False
                if attempt.timed_out:
                    conclusion.failure_reason = "timeout"
                if not attempt.terminal_observed:
                    if attempt.received_signal == signal.SIGINT:
                        conclusion.failure_reason = "cancelled"
                        attempt_cancelled = True
                    elif attempt.received_signal == signal.SIGTERM:
                        conclusion.failure_reason = "terminated"
                        attempt_cancelled = True
                if (
                    conclusion.exit_code != 0
                    and conclusion.failure_reason is None
                    and attempt.drain_error is not None
                ):
                    conclusion.failure_reason = attempt.drain_error

                _persist_attempt_artifacts(
                    artifacts=artifacts,
                    spawn_id=run.spawn_id,
                    log_dir=log_dir,
                    secrets=secrets,
                )
                if report_path.exists():
                    redacted_report = redact_secret_bytes(report_path.read_bytes(), secrets)
                    atomic_write_bytes(report_path, redacted_report)
                    artifacts.put(make_artifact_key(run.spawn_id, REPORT_FILENAME), redacted_report)

                streaming_extractor = StreamingExtractor(
                    connection=attempt.connection,
                    bundle=harness_bundle,
                    spec=spec,
                    launch_env=child_env,
                    child_cwd=child_cwd,
                    runtime_root=runtime_root,
                )
                extraction = enrich_finalize(
                    artifacts=artifacts,
                    extractor=streaming_extractor,
                    spawn_id=run.spawn_id,
                    log_dir=log_dir,
                    model_id=run.model,
                    harness_id=resolved_harness_id,
                    project_root=project_root,
                    secrets=secrets,
                    failure_reason=conclusion.failure_reason,
                )
                conclusion.extracted = extraction

                if (
                    _read_cancel_intent(runtime_root, run.spawn_id) is not None
                    and not extraction.durable_report_completion
                ):
                    _apply_cancel_intent_to_conclusion(
                        conclusion,
                        runtime_root=runtime_root,
                        spawn_id=run.spawn_id,
                    )
                    break

                # I-4: adapter observe_session_id() remains the sole post-attempt
                # discovery callsite. Streaming connections may report a known
                # session id earlier through ConnectionConfig.session_id_observer.
                extracted_harness_session_id = (
                    harness.observe_session_id(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        current_session_id=observed_harness_session_id,
                        connection_session_id=(
                            attempt.connection.session_id
                            if attempt.connection is not None
                            else None
                        ),
                        project_root=project_root,
                        started_at_epoch=started_at_epoch,
                        expected_session_id=observed_harness_session_id,
                    )
                    or ""
                )
                if extracted_harness_session_id:
                    try:
                        _record_harness_session_id(extracted_harness_session_id)
                    except Exception:
                        logger.warning(
                            "Harness session ID observer failed.",
                            spawn_id=str(run.spawn_id),
                            harness_id=str(harness.id),
                            exc_info=True,
                        )

                if attempt_cancelled:
                    if attempt.received_signal is not None:
                        conclusion.exit_code = signal_to_exit_code(attempt.received_signal) or 130
                    break

                if attempt.budget_breach is not None:
                    conclusion.failure_reason = "budget_exceeded"
                    conclusion.exit_code = DEFAULT_INFRA_EXIT_CODE
                    _append_budget_exceeded_event(run=run, breach=attempt.budget_breach)
                    break

                if (
                    budget_tracker is not None
                    and extraction.usage.total_cost_usd is not None
                    and budget_tracker.observe_cost(extraction.usage.total_cost_usd) is not None
                ):
                    conclusion.failure_reason = "budget_exceeded"
                    breach = budget_tracker.check()
                    if breach is not None:
                        _append_budget_exceeded_event(run=run, breach=breach)
                    conclusion.exit_code = DEFAULT_INFRA_EXIT_CODE
                    break

                if attempt.terminated_by_inactivity:
                    # Inactivity is terminal: either we recovered a durable report
                    # (success) or we finalize as "stalled". Never fall through to the
                    # generic retry classifier — re-running a stalled cursor turn would
                    # redo already-completed work.
                    exit_override, failure_override = _inactivity_terminal_outcome(
                        extraction
                    )
                    if exit_override is not None:
                        conclusion.exit_code = exit_override
                    conclusion.failure_reason = failure_override
                    break

                if (
                    conclusion.exit_code == 0
                    and _spawn_kind(runtime_root, run.spawn_id) == "child"
                    and extraction.report.content is None
                ):
                    conclusion.failure_reason = "missing_report"

                # A lingering Codex app-server can require watchdog-driven cleanup even after
                # the spawn has already written a durable report. Treat that as terminal
                # success here so the retry classifier never turns the synthetic exit code
                # from `stop_spawn()` back into another failed attempt.
                if attempt.terminated_by_report_watchdog and extraction.durable_report_completion:
                    conclusion.exit_code = 0
                    conclusion.failure_reason = None
                    break

                if extraction.output_is_empty:
                    if conclusion.exit_code == 0:
                        conclusion.exit_code = 1
                        conclusion.failure_reason = "empty_output"
                        break
                    if _artifact_is_zero_bytes(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        filename=HISTORY_FILENAME,
                    ) and _artifact_is_zero_bytes(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        filename=STDERR_FILENAME,
                    ):
                        _write_structured_failure_artifact(
                            artifacts=artifacts,
                            spawn_id=run.spawn_id,
                            output_log_path=output_log_path,
                            exit_code=conclusion.exit_code,
                            failure_reason=conclusion.failure_reason,
                            timed_out=attempt.timed_out,
                        )

                if conclusion.exit_code == 0:
                    guardrail_result = run_guardrails(
                        guardrails,
                        spawn_id=run.spawn_id,
                        cwd=child_cwd,
                        env=child_env,
                        report_path=extraction.report_path,
                        output_log_path=output_log_path,
                        timeout_seconds=guardrail_timeout_seconds,
                    )
                    if guardrail_result.ok:
                        break

                    conclusion.failure_reason = "guardrail_failed"
                    guardrail_text = _guardrail_failure_text(guardrail_result.failures)
                    _append_text_to_stderr_artifact(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        text=guardrail_text,
                        secrets=secrets,
                    )

                    if _retry_blocked_after_pi_child_started(
                        harness_id=resolved_harness_id,
                        runtime_root=runtime_root,
                        current_spawn_id=run.spawn_id,
                    ):
                        conclusion.exit_code = 1
                        break

                    if conclusion.retries_attempted >= max_retries:
                        conclusion.exit_code = 1
                        break

                    conclusion.retries_attempted += 1
                    conclusion.exit_code = 1
                    logger.info(
                        "Retrying after guardrail failure.",
                        spawn_id=str(run.spawn_id),
                        harness_id=str(harness.id),
                        retries_attempted=conclusion.retries_attempted,
                        max_retries=max_retries,
                        guardrail_failures=[
                            f"{item.script}:{item.exit_code}" for item in guardrail_result.failures
                        ],
                    )
                    if retry_backoff_seconds > 0:
                        cancelled_during_backoff = await _sleep_retry_backoff_or_cancel(
                            delay_seconds=retry_backoff_seconds
                            * conclusion.retries_attempted,
                            shutdown_event=shutdown_event,
                            runtime_root=runtime_root,
                            spawn_id=run.spawn_id,
                        )
                        if cancelled_during_backoff:
                            _apply_cancel_intent_to_conclusion(
                                conclusion,
                                runtime_root=runtime_root,
                                spawn_id=run.spawn_id,
                            )
                            break
                    continue

                stderr_key = make_artifact_key(run.spawn_id, STDERR_FILENAME)
                stderr_text = (
                    artifacts.get(stderr_key).decode("utf-8", errors="ignore")
                    if artifacts.exists(stderr_key)
                    else ""
                )
                category = classify_error(
                    conclusion.exit_code,
                    stderr_text,
                    timed_out=attempt.timed_out,
                    failure_message=attempt.drain_error,
                )
                if attempt.timed_out:
                    conclusion.failure_reason = "timeout"
                elif category == ErrorCategory.STRATEGY_CHANGE:
                    conclusion.failure_reason = "strategy_change"

                # Retrying after Pi already launched lifecycle-managed subspawn work is unsafe:
                # children cannot be re-adopted by a new parent retry attempt.
                if _retry_blocked_after_pi_child_started(
                    harness_id=resolved_harness_id,
                    runtime_root=runtime_root,
                    current_spawn_id=run.spawn_id,
                ):
                    break

                if attempt.authoritative_terminal_status is not None:
                    break

                if not should_retry(
                    exit_code=conclusion.exit_code,
                    stderr=stderr_text,
                    failure_message=attempt.drain_error,
                    timed_out=attempt.timed_out,
                    retries_attempted=conclusion.retries_attempted,
                    max_retries=max_retries,
                ):
                    break

                conclusion.retries_attempted += 1
                logger.info(
                    "Retrying failed run attempt.",
                    spawn_id=str(run.spawn_id),
                    harness_id=str(harness.id),
                    exit_code=conclusion.exit_code,
                    retries_attempted=conclusion.retries_attempted,
                    max_retries=max_retries,
                    error_category=str(category),
                )
                if retry_backoff_seconds > 0:
                    cancelled_during_backoff = await _sleep_retry_backoff_or_cancel(
                        delay_seconds=retry_backoff_seconds * conclusion.retries_attempted,
                        shutdown_event=shutdown_event,
                        runtime_root=runtime_root,
                        spawn_id=run.spawn_id,
                    )
                    if cancelled_during_backoff:
                        _apply_cancel_intent_to_conclusion(
                            conclusion,
                            runtime_root=runtime_root,
                            spawn_id=run.spawn_id,
                        )
                        break
        except asyncio.CancelledError:
            _record_lifecycle("task_cancelled")
            conclusion.exit_code = 130
            conclusion.failure_reason = "cancelled"
        except Exception as exc:
            _record_lifecycle(
                "exception",
                exception_type=type(exc).__name__,
                exception=str(exc),
            )
            logger.exception(
                "Streaming spawn execution failed with infrastructure error.",
                spawn_id=str(run.spawn_id),
                harness_id=str(launch_context.harness.id),
            )
            conclusion.exit_code = DEFAULT_INFRA_EXIT_CODE
            conclusion.failure_reason = "infrastructure_error"
    except Exception as exc:
        if lifecycle_path is not None:
            _append_runner_lifecycle_event(
                lifecycle_path,
                clock=resolved_clock,
                event="exception",
                phase=runner_phase[0],
                exception_type=type(exc).__name__,
                exception=str(exc),
            )
        conclusion.exit_code = DEFAULT_INFRA_EXIT_CODE
        conclusion.failure_reason = "infrastructure_error"
        logger.exception(
            "Streaming spawn setup failed.",
            spawn_id=str(run.spawn_id),
            harness_id=str(launch_context.harness.id),
        )
    finally:
        runner_phase[0] = "finalizing"
        if lifecycle_path is not None:
            _append_runner_lifecycle_event(
                lifecycle_path,
                clock=resolved_clock,
                event="finalizing",
                phase=runner_phase[0],
            )
        if signal_cleanup is not None:
            signal_cleanup()
        if manager is not None:
            with suppress(Exception):
                await manager.shutdown(status="cancelled", exit_code=1, error="shutdown")
        try:
            duration_seconds = resolved_clock.monotonic() - started_at
        except Exception:
            duration_seconds = 0.0
        if lifecycle_service is None:
            lifecycle_service = build_spawn_lifecycle_service_from_roots(
                project_root,
                runtime_root,
            )
        finalized_usage = conclusion.extracted.usage if conclusion.extracted is not None else None
        terminal_facts = conclusion.terminal_facts(received_signal=received_signal[0])
        with signal_coordinator().mask_sigterm():
            spawn_service = build_spawn_application_service_from_roots(
                project_root,
                runtime_root,
                lifecycle=lifecycle_service,
                spawn_manager=manager,
            )
            execution_outcome = await spawn_service.complete_execution(
                run.spawn_id,
                terminal_facts,
                origin="runner",
                duration_secs=duration_seconds,
                usage=finalized_usage,
            )
            conclusion.exit_code = execution_outcome.resolved.exit_code
            conclusion.failure_reason = execution_outcome.resolved.error
            outcome = execution_outcome.completion
            if outcome.entered_finalizing:
                try:
                    resolved_heartbeat_touch()
                except Exception:
                    logger.warning(
                        "Failed to touch heartbeat after entering finalizing; "
                        "terminal finalize already written.",
                        spawn_id=str(run.spawn_id),
                        harness_id=str(launch_context.harness.id),
                        exc_info=True,
                    )
            elif not outcome.wrote:
                logger.info(
                    "Runner finalize skipped; spawn already terminal or missing.",
                    spawn_id=str(run.spawn_id),
                    harness_id=str(launch_context.harness.id),
                )

        runner_phase[0] = "completed"
        lifecycle_active[0] = False
        if lifecycle_path is not None:
            _append_runner_lifecycle_event(
                lifecycle_path,
                clock=resolved_clock,
                event="runner_completed",
                phase=runner_phase[0],
                exit_code=conclusion.exit_code,
            )
        if atexit_callback is not None:
            atexit.unregister(atexit_callback)

    return conclusion.exit_code


__all__ = [
    "DEFAULT_GUARDRAIL_TIMEOUT_SECONDS",
    "StreamingRunConclusion",
    "TerminalEventOutcome",
    "execute_with_streaming",
    "run_streaming_spawn",
]
