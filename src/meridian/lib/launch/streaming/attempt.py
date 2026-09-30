"""One streaming harness attempt and its bounded async mechanics."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import TypeAdapter

from meridian.lib.bootstrap.services import build_spawn_lifecycle_service_from_roots
from meridian.lib.core.clock import Clock
from meridian.lib.core.domain import Spawn, SpawnStatus, TerminalSpawnStatus
from meridian.lib.core.native_identity import NativeIdentityError
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import StreamEvent
from meridian.lib.harness.common import parse_json_stream_event, unwrap_event_payload
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.harness.connections.errors import (
    ConnectionStartFailure,
    TurnSubmission,
)
from meridian.lib.harness.semantics import NormalizedHarnessEvent, TerminalEventOutcome
from meridian.lib.launch.constants import (
    CURSOR_INACTIVITY_TIMEOUT_SECONDS,
    DEFAULT_INFRA_EXIT_CODE,
    REPORT_FILENAME,
    REPORT_WATCHDOG_GRACE_SECONDS,
    REPORT_WATCHDOG_POLL_SECONDS,
    SUBPROCESS_REPORT_WATCHDOG_POLL_SECONDS,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.signals import signal_coordinator, signal_to_exit_code
from meridian.lib.launch.streaming.heartbeat import FileHeartbeat, HeartbeatTouch
from meridian.lib.launch.streaming.terminal_arbitrator import TriggerKind, arbitrate_terminal
from meridian.lib.safety.budget import BudgetBreach, LiveBudgetTracker
from meridian.lib.state import paths as state_paths
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn.model import LaunchMode
from meridian.lib.streaming.spawn_manager import DrainOutcome, SpawnManager

if TYPE_CHECKING:
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.harness.connections.base import RawHarnessEvent

logger = structlog.get_logger(__name__)
_HEARTBEAT_INTERVAL_SECS = 30.0

@dataclass(frozen=True)
class AttemptRuntime:
    connection: HarnessConnection[Any] | None
    drain_exit_code: int
    drain_error: str | None
    timed_out: bool
    received_signal: signal.Signals | None
    budget_breach: BudgetBreach | None
    terminated_by_report_watchdog: bool
    terminated_by_inactivity: bool = False
    cancelled_by_request: bool = False
    terminal_outcome: TerminalEventOutcome | None = None
    authoritative_terminal_status: TerminalSpawnStatus | None = None
    start_error: str | None = None
    start_failure: ConnectionStartFailure | None = None
    turn_submission: TurnSubmission = TurnSubmission.UNKNOWN
    identity_error: NativeIdentityError | None = None

    @property
    def terminal_observed(self) -> bool:
        return self.terminal_outcome is not None or self.authoritative_terminal_status is not None


class StartupPhaseTimeout(TimeoutError):
    """The backend boot/connection/session-handshake phase exceeded its bound."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"startup phase timeout after {timeout_seconds:.3f}s")


def touch_heartbeat_file(
    runtime_root: Path,
    spawn_id: SpawnId,
    *,
    clock: Clock | None = None,
) -> None:
    FileHeartbeat(
        state_paths.heartbeat_path(runtime_root, spawn_id),
        clock=clock,
    ).touch()


def install_signal_handlers(
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


def _line_from_harness_event(event: RawHarnessEvent) -> str:
    if event.raw_text is not None and event.raw_text.strip():
        return event.raw_text
    payload: dict[str, object] = dict(event.payload)
    payload.setdefault("event", event.event_type)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _observe_budget_from_event(
    *,
    budget_tracker: LiveBudgetTracker | None,
    event: RawHarnessEvent,
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
    subscriber: asyncio.Queue[NormalizedHarnessEvent | None],
    budget_tracker: LiveBudgetTracker | None,
    budget_signal: asyncio.Event,
    budget_breach_holder: list[BudgetBreach | None],
    event_observer: Callable[[StreamEvent], None] | None,
    stream_stdout_to_terminal: bool,
    terminal_event_future: asyncio.Future[TerminalEventOutcome] | None = None,
    last_event_at: list[float] | None = None,
) -> None:
    while True:
        normalized_event = await subscriber.get()
        if normalized_event is None:
            return
        event = normalized_event.raw

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
            event_outcome = normalized_event.semantics.terminal
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

    await manager.stop_spawn(
        spawn_id, status=SpawnStatus.CANCELLED, exit_code=1, error="report_watchdog"
    )
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
    await manager.stop_spawn(
        spawn_id, status=SpawnStatus.FAILED, exit_code=1, error="inactivity_stall"
    )
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
    event_hook: Callable[[RawHarnessEvent], None] | None = None,
) -> HarnessConnection[Any]:
    """Start a managed connection within the shared startup-phase bound."""

    try:
        async with asyncio.timeout(timeout_seconds):
            return await manager.start_spawn(config, run_spec, event_hook=event_hook)
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
    on_running: Callable[[HarnessConnection[Any]], None] | None = None,
    event_hook: Callable[[RawHarnessEvent], None] | None = None,
) -> DrainOutcome:
    """Run one streaming spawn to completion without spawn-store finalization.

    Callers are responsible for resolving *spec* via ``build_launch_context()``
    before calling this function.  I-8 (executors stay mechanism-only): this
    executor accepts a fully-composed spec and MUST NOT perform composition.
    """

    resolved_heartbeat_touch = heartbeat_touch or (
        lambda: touch_heartbeat_file(runtime_root, spawn_id)
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
    signal_cleanup = install_signal_handlers(loop, shutdown_event, received_signal)

    completion_task: asyncio.Task[DrainOutcome | None] | None = None
    signal_task: asyncio.Task[bool] | None = None
    consume_task: asyncio.Task[None] | None = None
    terminal_event_future: asyncio.Future[TerminalEventOutcome] | None = None
    terminal_outcome: TerminalEventOutcome | None = None
    subscriber: asyncio.Queue[NormalizedHarnessEvent | None] | None = None
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
            event_hook=event_hook,
        )
        if on_running is not None:
            on_running(connection)
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
                status=decision.synthetic_status or SpawnStatus.CANCELLED,
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
            await manager.join_teardown(spawn_id)
            with suppress(Exception):
                await manager.shutdown(status=SpawnStatus.CANCELLED, exit_code=1, error="shutdown")


async def run_streaming_attempt(
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
    on_running: Callable[[HarnessConnection[Any]], None] | None = None,
    event_hook: Callable[[RawHarnessEvent], None] | None = None,
) -> AttemptRuntime:
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
    subscriber: asyncio.Queue[NormalizedHarnessEvent | None] | None = None
    connection: HarnessConnection[Any] | None = None
    drain_exit_code = DEFAULT_INFRA_EXIT_CODE
    drain_error: str | None = None
    timed_out = False
    terminated_by_report_watchdog = False
    terminated_by_inactivity = False
    cancelled_by_request = False
    terminal_outcome: TerminalEventOutcome | None = None
    authoritative_terminal_status: TerminalSpawnStatus | None = None
    recording_selection = False
    start_error: str | None = None
    start_failure: ConnectionStartFailure | None = None
    turn_submission = TurnSubmission.UNKNOWN
    identity_error: NativeIdentityError | None = None
    try:
        if runner_phase is not None:
            runner_phase[0] = "starting_harness"
        connection = await _start_spawn_with_timeout(
            manager=manager,
            config=config,
            run_spec=run_spec,
            timeout_seconds=startup_timeout_seconds,
            event_hook=event_hook,
        )
        turn_submission = TurnSubmission.SUBMITTED
        terminal_event_capture = (
            terminal_event_future
            if manager.raw_terminal_frames_are_authoritative(run.spawn_id)
            else None
        )
        if on_running is not None:
            recording_selection = True
            on_running(connection)
            recording_selection = False
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
                status=SpawnStatus.FAILED,
                exit_code=DEFAULT_INFRA_EXIT_CODE,
                error="budget_exceeded",
            )
            drain_exit_code = DEFAULT_INFRA_EXIT_CODE
        elif decision.trigger == TriggerKind.TIMEOUT:
            timed_out = True
            await manager.stop_spawn(
                run.spawn_id,
                status=SpawnStatus.TIMED_OUT,
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
                status=decision.synthetic_status or SpawnStatus.CANCELLED,
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
                    authoritative_terminal_status = TypeAdapter(
                        TerminalSpawnStatus
                    ).validate_python(drain_outcome.status)
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
    except ConnectionStartFailure as exc:
        start_error = str(exc.cause)
        start_failure = exc
        turn_submission = exc.turn_submission
        if isinstance(exc.cause, NativeIdentityError):
            identity_error = exc.cause
    except NativeIdentityError as exc:
        start_error, identity_error = str(exc), exc
    except Exception as exc:
        if recording_selection:
            raise
        start_error = str(exc)
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
        if start_error is not None:
            await manager.stop_spawn(
                run.spawn_id, status=SpawnStatus.FAILED, exit_code=1, error=start_error
            )
        # Terminal publication hides the active connection while teardown can
        # still be publishing its native quit. Join that cleanup before reading
        # the run boundary. Joining does not publish a synthetic cancellation.
        await manager.join_teardown(run.spawn_id)

    if start_error is not None:
        drain_exit_code, drain_error, timed_out = DEFAULT_INFRA_EXIT_CODE, start_error, False
        terminal_outcome, authoritative_terminal_status = None, None
    return AttemptRuntime(
        connection=connection,
        drain_exit_code=drain_exit_code,
        drain_error=drain_error,
        timed_out=timed_out,
        received_signal=received_signal[0],
        budget_breach=budget_breach_holder[0],
        terminated_by_report_watchdog=terminated_by_report_watchdog,
        terminated_by_inactivity=terminated_by_inactivity,
        cancelled_by_request=cancelled_by_request,
        terminal_outcome=terminal_outcome,
        authoritative_terminal_status=authoritative_terminal_status,
        start_error=start_error,
        start_failure=start_failure,
        turn_submission=turn_submission,
        identity_error=identity_error,
    )
