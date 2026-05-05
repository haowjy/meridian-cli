"""Bidirectional spawn execution with runner-owned finalization and retries."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.clock import Clock, RealClock
from meridian.lib.core.domain import Spawn
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.spawn_lifecycle import (
    has_durable_report_completion,
    resolve_execution_terminal_state,
)
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import StreamEvent
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.claude_preflight import ensure_claude_session_accessible
from meridian.lib.harness.claude_utils import extract_session_id_from_args
from meridian.lib.harness.common import parse_json_stream_event, unwrap_event_payload
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.harness.extractor import StreamingExtractor
from meridian.lib.launch.constants import (
    DEFAULT_INFRA_EXIT_CODE,
    HISTORY_FILENAME,
    REPORT_FILENAME,
    REPORT_WATCHDOG_GRACE_SECONDS,
    REPORT_WATCHDOG_POLL_SECONDS,
    STDERR_FILENAME,
    TOKENS_FILENAME,
)
from meridian.lib.launch.context import LaunchContext
from meridian.lib.launch.errors import ErrorCategory, classify_error, should_retry
from meridian.lib.launch.extract import (
    FinalizeExtraction,
    enrich_finalize,
    reset_finalize_attempt_artifacts,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.request import SpawnRequest
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
from meridian.lib.launch.streaming.decision import (
    TerminalEventOutcome,
    terminal_event_outcome,
)
from meridian.lib.launch.streaming.heartbeat import FileHeartbeat, HeartbeatTouch
from meridian.lib.safety.budget import Budget, BudgetBreach, LiveBudgetTracker
from meridian.lib.safety.guardrails import run_guardrails
from meridian.lib.safety.redaction import SecretSpec, redact_secret_bytes
from meridian.lib.state import paths as state_paths
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import ArtifactStore, make_artifact_key
from meridian.lib.state.atomic import atomic_write_bytes
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.spawn_store import (
    BACKGROUND_LAUNCH_MODE,
    FOREGROUND_LAUNCH_MODE,
)
from meridian.lib.streaming.spawn_manager import DrainOutcome, SpawnManager

if TYPE_CHECKING:
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.harness.connections.base import HarnessEvent

_DEFAULT_CONFIG = MeridianConfig()
DEFAULT_GUARDRAIL_TIMEOUT_SECONDS = _DEFAULT_CONFIG.guardrail_timeout_minutes * 60.0
logger = structlog.get_logger(__name__)
_TERMINAL_EVENT_GRACE_SECONDS = 0.5
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
    terminal_observed: bool = False
    start_error: str | None = None


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
) -> list[signal.Signals]:
    installed: list[signal.Signals] = []

    def _handle_signal(signum: signal.Signals) -> None:
        if received_signal[0] is None:
            received_signal[0] = signum
        shutdown_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, _handle_signal, signum)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            continue
    return installed


def _remove_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    installed: Iterable[signal.Signals],
) -> None:
    for signum in installed:
        with suppress(Exception):
            loop.remove_signal_handler(signum)


def _truncate_attempt_logs(log_dir: Path) -> None:
    for name in (
        HISTORY_FILENAME,
        STDERR_FILENAME,
        TOKENS_FILENAME,
        REPORT_FILENAME,
    ):
        target = log_dir / name
        if target.exists():
            target.unlink()


def _persist_attempt_artifacts(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    log_dir: Path,
    secrets: tuple[SecretSpec, ...],
) -> None:
    for name in (HISTORY_FILENAME, STDERR_FILENAME, TOKENS_FILENAME):
        source = log_dir / name
        if not source.exists():
            continue
        payload = source.read_bytes()
        if name in {HISTORY_FILENAME, STDERR_FILENAME}:
            payload = redact_secret_bytes(payload, secrets)
        artifacts.put(make_artifact_key(spawn_id, name), payload)


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


async def _await_terminal_outcome_after_completion(
    *,
    completion_task: asyncio.Task[DrainOutcome | None],
    terminal_event_future: asyncio.Future[TerminalEventOutcome],
    grace_seconds: float = _TERMINAL_EVENT_GRACE_SECONDS,
) -> TerminalEventOutcome | None:
    if terminal_event_future.done():
        return terminal_event_future.result()
    if not completion_task.done():
        return None
    try:
        return await asyncio.wait_for(
            asyncio.shield(terminal_event_future),
            timeout=grace_seconds,
        )
    except TimeoutError:
        return None


async def _consume_subscriber_events(
    *,
    subscriber: asyncio.Queue[HarnessEvent | None],
    budget_tracker: LiveBudgetTracker | None,
    budget_signal: asyncio.Event,
    budget_breach_holder: list[BudgetBreach | None],
    event_observer: Callable[[StreamEvent], None] | None,
    stream_stdout_to_terminal: bool,
    terminal_event_future: asyncio.Future[TerminalEventOutcome] | None = None,
) -> None:
    while True:
        event = await subscriber.get()
        if event is None:
            return

        if budget_breach_holder[0] is None:
            breach = _observe_budget_from_event(
                budget_tracker=budget_tracker,
                event=event,
            )
            if breach is not None:
                budget_breach_holder[0] = breach
                budget_signal.set()

        if terminal_event_future is not None and not terminal_event_future.done():
            terminal_outcome = terminal_event_outcome(event)
            if terminal_outcome is not None:
                terminal_event_future.set_result(terminal_outcome)

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


async def run_streaming_spawn(
    *,
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
    runtime_root: Path,
    project_root: Path,
    spawn_id: SpawnId,
    stream_to_terminal: bool = False,
    heartbeat_touch: HeartbeatTouch | None = None,
    heartbeat_interval_secs: float = _HEARTBEAT_INTERVAL_SECS,
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
    installed_signals = _install_signal_handlers(loop, shutdown_event, received_signal)

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
    lifecycle_service = create_lifecycle_service(project_root, runtime_root)
    try:
        await manager.start_spawn(config, run_spec)
        await manager._start_heartbeat(spawn_id)  # pyright: ignore[reportPrivateUsage]
        subscriber = manager.subscribe(spawn_id)
        if subscriber is None:
            raise RuntimeError("failed to subscribe to spawn stream")

        terminal_event_future = loop.create_future()
        completion_task = asyncio.create_task(manager.wait_for_completion(spawn_id))
        consume_task = asyncio.create_task(
            _consume_subscriber_events(
                subscriber=subscriber,
                budget_tracker=None,
                budget_signal=asyncio.Event(),
                budget_breach_holder=[None],
                event_observer=None,
                stream_stdout_to_terminal=stream_to_terminal,
                terminal_event_future=terminal_event_future,
            )
        )
        signal_task = asyncio.create_task(shutdown_event.wait())

        done, _ = await asyncio.wait(
            {
                cast("asyncio.Task[object]", completion_task),
                cast("asyncio.Task[object]", signal_task),
                cast("asyncio.Future[object]", terminal_event_future),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        if terminal_event_future in done:
            terminal_outcome = terminal_event_future.result()
            await manager.stop_spawn(
                spawn_id,
                status=terminal_outcome.status,
                exit_code=terminal_outcome.exit_code,
                error=terminal_outcome.error,
            )
        elif completion_task in done:
            # Completion and signal can resolve in the same wakeup while a terminal
            # frame is still queued; run completion grace first to preserve terminal
            # precedence when that frame lands shortly after drain completion.
            terminal_outcome = await _await_terminal_outcome_after_completion(
                completion_task=completion_task,
                terminal_event_future=terminal_event_future,
            )
            if terminal_outcome is not None:
                await manager.stop_spawn(
                    spawn_id,
                    status=terminal_outcome.status,
                    exit_code=terminal_outcome.exit_code,
                    error=terminal_outcome.error,
                )
        elif signal_task in done and shutdown_event.is_set():
            if completion_task.done():
                terminal_outcome = await _await_terminal_outcome_after_completion(
                    completion_task=completion_task,
                    terminal_event_future=terminal_event_future,
                )
                if terminal_outcome is not None:
                    await manager.stop_spawn(
                        spawn_id,
                        status=terminal_outcome.status,
                        exit_code=terminal_outcome.exit_code,
                        error=terminal_outcome.error,
                    )
            else:
                signal_exit = signal_to_exit_code(received_signal[0]) or 130
                await manager.stop_spawn(
                    spawn_id,
                    status="cancelled",
                    exit_code=signal_exit,
                    error="cancelled",
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
            lifecycle_service.record_exited(
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
            _remove_signal_handlers(loop, installed_signals)
            with suppress(Exception):
                await manager.shutdown(status="cancelled", exit_code=1, error="shutdown")


async def _run_streaming_attempt(
    *,
    run: Spawn,
    runtime_root: Path,
    launch_mode: spawn_store.LaunchMode,
    log_dir: Path,
    manager: SpawnManager,
    config: ConnectionConfig,
    run_spec: ResolvedLaunchSpec,
    budget_tracker: LiveBudgetTracker | None,
    signal_event: asyncio.Event,
    received_signal: list[signal.Signals | None],
    timeout_seconds: float | None,
    event_observer: Callable[[StreamEvent], None] | None,
    stream_stdout_to_terminal: bool,
) -> _AttemptRuntime:
    completion_task: asyncio.Task[DrainOutcome | None] | None = None
    timeout_task: asyncio.Task[None] | None = None
    signal_task: asyncio.Task[bool] | None = None
    budget_task: asyncio.Task[bool] | None = None
    watchdog_task: asyncio.Task[bool] | None = None
    consume_task: asyncio.Task[None] | None = None
    completion_event = asyncio.Event()
    budget_signal = asyncio.Event()
    budget_breach_holder: list[BudgetBreach | None] = [None]
    terminal_event_future: asyncio.Future[TerminalEventOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    subscriber: asyncio.Queue[HarnessEvent | None] | None = None
    connection: HarnessConnection[Any] | None = None
    drain_exit_code = DEFAULT_INFRA_EXIT_CODE
    drain_error: str | None = None
    timed_out = False
    terminated_by_report_watchdog = False
    terminal_outcome: TerminalEventOutcome | None = None
    lifecycle_service = create_lifecycle_service(manager.project_root, runtime_root)

    try:
        connection = await manager.start_spawn(config, run_spec)
        await manager._start_heartbeat(run.spawn_id)  # pyright: ignore[reportPrivateUsage]
        lifecycle_service.mark_running(
            run.spawn_id,
            launch_mode=launch_mode,
            worker_pid=connection.subprocess_pid,
        )

        subscriber = manager.subscribe(run.spawn_id)
        if subscriber is None:
            raise RuntimeError("failed to subscribe to spawn stream")

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
                terminal_event_future=terminal_event_future,
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

        wait_tasks: set[asyncio.Future[object]] = {
            cast("asyncio.Task[object]", completion_task),
            cast("asyncio.Task[object]", signal_task),
            cast("asyncio.Task[object]", watchdog_task),
            cast("asyncio.Future[object]", terminal_event_future),
        }
        if budget_task is not None:
            wait_tasks.add(cast("asyncio.Task[object]", budget_task))
        if timeout_task is not None:
            wait_tasks.add(cast("asyncio.Task[object]", timeout_task))

        done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        if terminal_event_future in done:
            terminal_outcome = terminal_event_future.result()
            await manager.stop_spawn(
                run.spawn_id,
                status=terminal_outcome.status,
                exit_code=terminal_outcome.exit_code,
                error=terminal_outcome.error,
            )
            drain_exit_code = terminal_outcome.exit_code
            drain_error = terminal_outcome.error
        elif budget_task is not None and budget_task in done and budget_signal.is_set():
            await manager.stop_spawn(
                run.spawn_id,
                status="failed",
                exit_code=DEFAULT_INFRA_EXIT_CODE,
                error="budget_exceeded",
            )
            drain_exit_code = DEFAULT_INFRA_EXIT_CODE
        elif timeout_task is not None and timeout_task in done:
            timed_out = True
            await manager.stop_spawn(
                run.spawn_id,
                status="failed",
                exit_code=3,
                error="timeout",
            )
            drain_exit_code = 3
        elif watchdog_task in done:
            terminated_by_report_watchdog = bool(watchdog_task.result())
        elif completion_task in done:
            # If completion and signal resolve together, always run the bounded
            # terminal grace window so late-arriving terminal frames can win.
            terminal_outcome = await _await_terminal_outcome_after_completion(
                completion_task=completion_task,
                terminal_event_future=terminal_event_future,
            )
            if terminal_outcome is not None:
                await manager.stop_spawn(
                    run.spawn_id,
                    status=terminal_outcome.status,
                    exit_code=terminal_outcome.exit_code,
                    error=terminal_outcome.error,
                )
                drain_exit_code = terminal_outcome.exit_code
                drain_error = terminal_outcome.error
        elif signal_task in done and signal_event.is_set():
            if completion_task.done():
                terminal_outcome = await _await_terminal_outcome_after_completion(
                    completion_task=completion_task,
                    terminal_event_future=terminal_event_future,
                )
                if terminal_outcome is not None:
                    await manager.stop_spawn(
                        run.spawn_id,
                        status=terminal_outcome.status,
                        exit_code=terminal_outcome.exit_code,
                        error=terminal_outcome.error,
                    )
                    drain_exit_code = terminal_outcome.exit_code
                    drain_error = terminal_outcome.error
            else:
                signal_exit = signal_to_exit_code(received_signal[0]) or 130
                await manager.stop_spawn(
                    run.spawn_id,
                    status="cancelled",
                    exit_code=signal_exit,
                    error="cancelled",
                )
                drain_exit_code = signal_exit

        if watchdog_task in done and not terminated_by_report_watchdog:
            terminated_by_report_watchdog = bool(watchdog_task.result())

        drain_outcome = await completion_task
        if drain_outcome is not None and terminal_outcome is None:
            drain_exit_code = drain_outcome.exit_code
            drain_error = drain_outcome.error

        # The watchdog resolves the completion future mid-flight inside
        # stop_spawn(), so completion_task can finish before watchdog_task.
        # Give the watchdog a brief window to land and reconcile the flag.
        if not terminated_by_report_watchdog:
            if watchdog_task.done():
                with suppress(Exception):
                    terminated_by_report_watchdog = bool(watchdog_task.result())
            elif drain_outcome is not None and drain_outcome.error == "report_watchdog":
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
            drain_error=None,
            timed_out=False,
            received_signal=received_signal[0],
            budget_breach=budget_breach_holder[0],
            terminated_by_report_watchdog=terminated_by_report_watchdog,
            terminal_observed=False,
            start_error=str(exc),
        )
    finally:
        if subscriber is not None:
            manager.unsubscribe(run.spawn_id)
        for task in (timeout_task, signal_task, budget_task, watchdog_task, consume_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if manager.get_connection(run.spawn_id) is not None:
            with suppress(Exception):
                await manager.stop_spawn(run.spawn_id)

    return _AttemptRuntime(
        connection=connection,
        drain_exit_code=drain_exit_code,
        drain_error=drain_error,
        timed_out=timed_out,
        received_signal=received_signal[0],
        budget_breach=budget_breach_holder[0],
        terminated_by_report_watchdog=terminated_by_report_watchdog,
        terminal_observed=terminal_outcome is not None,
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
    exit_code = DEFAULT_INFRA_EXIT_CODE
    failure_reason: str | None = None
    extracted: FinalizeExtraction | None = None
    terminated_after_completion = False
    final_attempt_terminal_observed = False
    retries_attempted = 0
    lifecycle_service: SpawnLifecycleService | None = None
    manager: SpawnManager | None = None
    installed_signals: list[signal.Signals] = []
    loop: asyncio.AbstractEventLoop | None = None
    received_signal: list[signal.Signals | None] = [None]

    try:
        log_dir = resolve_spawn_log_dir(project_root, run.spawn_id)
        output_log_path = log_dir / HISTORY_FILENAME
        report_path = launch_context.report_output_path

        timeout_seconds = (
            float(request.budget.timeout_secs) if request.budget.timeout_secs is not None else None
        )
        max_retries = max(request.retry.max_attempts - 1, 0)
        retry_backoff_seconds = request.retry.backoff_secs

        resolved_harness_id = launch_context.harness.id
        child_cwd = launch_context.child_cwd
        spec = launch_context.spec
        child_env = dict(launch_context.env)
        harness = launch_context.harness
        harness_bundle = get_harness_bundle(resolved_harness_id)

        spawn_store.update_spawn(
            runtime_root,
            run.spawn_id,
            execution_cwd=str(child_cwd),
        )

        if (
            harness.id == HarnessId.CLAUDE
            and request.session.requested_harness_session_id
            and request.session.source_execution_cwd
        ):
            ensure_claude_session_accessible(
                source_session_id=request.session.requested_harness_session_id,
                source_cwd=Path(request.session.source_execution_cwd),
                child_cwd=child_cwd,
            )
        tracer: DebugTracer | None = None
        if debug:
            from meridian.lib.observability.debug_tracer import DebugTracer

            tracer = DebugTracer(
                spawn_id=str(run.spawn_id),
                debug_path=log_dir / "debug.jsonl",
                echo_stderr=stream_stdout_to_terminal,
            )

        config = ConnectionConfig(
            spawn_id=run.spawn_id,
            harness_id=resolved_harness_id,
            prompt=spec.prompt,
            project_root=child_cwd,
            env_overrides=child_env,
            system=getattr(spec, "appended_system_prompt", None),
            timeout_seconds=timeout_seconds,
            debug_tracer=tracer,
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
        resolved_launch_mode: spawn_store.LaunchMode = (
            BACKGROUND_LAUNCH_MODE
            if spawn_row.launch_mode == BACKGROUND_LAUNCH_MODE
            else FOREGROUND_LAUNCH_MODE
        )

        materialized_session_id = (spec.continue_session_id or "").strip()
        observed_harness_session_id: str | None = None
        if resolved_harness_id == HarnessId.CLAUDE and not materialized_session_id:
            seeded_session_id = extract_session_id_from_args(spec.extra_args)
            if seeded_session_id:
                spawn_store.update_spawn(
                    runtime_root,
                    run.spawn_id,
                    harness_session_id=seeded_session_id,
                )
                observed_harness_session_id = seeded_session_id
                if harness_session_id_observer is not None:
                    harness_session_id_observer(seeded_session_id)
        if materialized_session_id and materialized_session_id != (
            request.session.requested_harness_session_id or ""
        ):
            spawn_store.update_spawn(
                runtime_root,
                run.spawn_id,
                harness_session_id=materialized_session_id,
            )
            observed_harness_session_id = materialized_session_id
            if harness_session_id_observer is not None:
                harness_session_id_observer(materialized_session_id)

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
        lifecycle_service = create_lifecycle_service(project_root, runtime_root)

        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        installed_signals = _install_signal_handlers(loop, shutdown_event, received_signal)

        try:
            while True:
                reset_finalize_attempt_artifacts(
                    artifacts=artifacts,
                    spawn_id=run.spawn_id,
                    log_dir=log_dir,
                )
                _truncate_attempt_logs(log_dir)

                if preflight_breach is not None:
                    exit_code = DEFAULT_INFRA_EXIT_CODE
                    failure_reason = "budget_exceeded"
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
                    event_observer=event_observer,
                    stream_stdout_to_terminal=stream_stdout_to_terminal,
                )
                exit_code = attempt.drain_exit_code
                terminated_after_completion = (
                    terminated_after_completion or attempt.terminated_by_report_watchdog
                )
                final_attempt_terminal_observed = attempt.terminal_observed
                if attempt.start_error is not None:
                    logger.warning(
                        "Failed to execute streaming spawn attempt.",
                        spawn_id=str(run.spawn_id),
                        harness_id=str(harness.id),
                        error=attempt.start_error,
                    )
                    failure_reason = attempt.start_error
                    _append_text_to_stderr_artifact(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        text=attempt.start_error,
                        secrets=secrets,
                    )
                attempt_cancelled = False
                if attempt.timed_out:
                    failure_reason = "timeout"
                if not attempt.terminal_observed:
                    if attempt.received_signal == signal.SIGINT:
                        failure_reason = "cancelled"
                        attempt_cancelled = True
                    elif attempt.received_signal == signal.SIGTERM:
                        failure_reason = "terminated"
                        attempt_cancelled = True
                elif exit_code != 0 and failure_reason is None and attempt.drain_error is not None:
                    failure_reason = attempt.drain_error

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
                extracted = enrich_finalize(
                    artifacts=artifacts,
                    extractor=streaming_extractor,
                    spawn_id=run.spawn_id,
                    log_dir=log_dir,
                    secrets=secrets,
                )

                # I-4: observe_session_id() is the sole observation callsite.
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
                if (
                    extracted_harness_session_id
                    and extracted_harness_session_id != observed_harness_session_id
                ):
                    try:
                        spawn_store.update_spawn(
                            runtime_root,
                            run.spawn_id,
                            harness_session_id=extracted_harness_session_id,
                        )
                        observed_harness_session_id = extracted_harness_session_id
                        if harness_session_id_observer is not None:
                            harness_session_id_observer(extracted_harness_session_id)
                    except Exception:
                        logger.warning(
                            "Harness session ID observer failed.",
                            spawn_id=str(run.spawn_id),
                            harness_id=str(harness.id),
                            exc_info=True,
                        )

                if attempt_cancelled:
                    if attempt.received_signal is not None:
                        exit_code = signal_to_exit_code(attempt.received_signal) or 130
                    break

                if attempt.budget_breach is not None:
                    failure_reason = "budget_exceeded"
                    exit_code = DEFAULT_INFRA_EXIT_CODE
                    _append_budget_exceeded_event(run=run, breach=attempt.budget_breach)
                    break

                if (
                    budget_tracker is not None
                    and extracted.usage.total_cost_usd is not None
                    and budget_tracker.observe_cost(extracted.usage.total_cost_usd) is not None
                ):
                    failure_reason = "budget_exceeded"
                    breach = budget_tracker.check()
                    if breach is not None:
                        _append_budget_exceeded_event(run=run, breach=breach)
                    exit_code = DEFAULT_INFRA_EXIT_CODE
                    break

                if (
                    exit_code == 0
                    and _spawn_kind(runtime_root, run.spawn_id) == "child"
                    and extracted.report.content is None
                ):
                    failure_reason = "missing_report"

                # A lingering Codex app-server can require watchdog-driven cleanup even after
                # the spawn has already written a durable report. Treat that as terminal
                # success here so the retry classifier never turns the synthetic exit code
                # from `stop_spawn()` back into another failed attempt.
                if attempt.terminated_by_report_watchdog and has_durable_report_completion(
                    extracted.report.content
                ):
                    exit_code = 0
                    failure_reason = None
                    break

                if extracted.output_is_empty:
                    if exit_code == 0:
                        exit_code = 1
                        failure_reason = "empty_output"
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
                            exit_code=exit_code,
                            failure_reason=failure_reason,
                            timed_out=attempt.timed_out,
                        )

                if exit_code == 0:
                    guardrail_result = run_guardrails(
                        guardrails,
                        spawn_id=run.spawn_id,
                        cwd=child_cwd,
                        env=child_env,
                        report_path=extracted.report_path,
                        output_log_path=output_log_path,
                        timeout_seconds=guardrail_timeout_seconds,
                    )
                    if guardrail_result.ok:
                        break

                    failure_reason = "guardrail_failed"
                    guardrail_text = _guardrail_failure_text(guardrail_result.failures)
                    _append_text_to_stderr_artifact(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        text=guardrail_text,
                        secrets=secrets,
                    )

                    if retries_attempted >= max_retries:
                        exit_code = 1
                        break

                    retries_attempted += 1
                    exit_code = 1
                    logger.warning(
                        "Retrying after guardrail failure.",
                        spawn_id=str(run.spawn_id),
                        harness_id=str(harness.id),
                        retries_attempted=retries_attempted,
                        max_retries=max_retries,
                        guardrail_failures=[
                            f"{item.script}:{item.exit_code}" for item in guardrail_result.failures
                        ],
                    )
                    if retry_backoff_seconds > 0:
                        await asyncio.sleep(retry_backoff_seconds * retries_attempted)
                    continue

                stderr_key = make_artifact_key(run.spawn_id, STDERR_FILENAME)
                stderr_text = (
                    artifacts.get(stderr_key).decode("utf-8", errors="ignore")
                    if artifacts.exists(stderr_key)
                    else ""
                )
                category = classify_error(
                    exit_code,
                    stderr_text,
                    timed_out=attempt.timed_out,
                )
                if attempt.timed_out:
                    failure_reason = "timeout"
                elif category == ErrorCategory.STRATEGY_CHANGE:
                    failure_reason = "strategy_change"

                if not should_retry(
                    exit_code=exit_code,
                    stderr=stderr_text,
                    timed_out=attempt.timed_out,
                    retries_attempted=retries_attempted,
                    max_retries=max_retries,
                ):
                    break

                retries_attempted += 1
                logger.warning(
                    "Retrying failed run attempt.",
                    spawn_id=str(run.spawn_id),
                    harness_id=str(harness.id),
                    exit_code=exit_code,
                    retries_attempted=retries_attempted,
                    max_retries=max_retries,
                    error_category=str(category),
                )
                if retry_backoff_seconds > 0:
                    await asyncio.sleep(retry_backoff_seconds * retries_attempted)
        except asyncio.CancelledError:
            exit_code = 130
            failure_reason = "cancelled"
        except Exception:
            logger.exception(
                "Streaming spawn execution failed with infrastructure error.",
                spawn_id=str(run.spawn_id),
                harness_id=str(launch_context.harness.id),
            )
            exit_code = DEFAULT_INFRA_EXIT_CODE
            failure_reason = "infrastructure_error"
    except Exception:
        exit_code = DEFAULT_INFRA_EXIT_CODE
        failure_reason = "infrastructure_error"
        logger.exception(
            "Streaming spawn setup failed.",
            spawn_id=str(run.spawn_id),
            harness_id=str(launch_context.harness.id),
        )
    finally:
        if loop is not None and installed_signals:
            _remove_signal_handlers(loop, installed_signals)
        if manager is not None:
            with suppress(Exception):
                await manager.shutdown(status="cancelled", exit_code=1, error="shutdown")
        try:
            duration_seconds = resolved_clock.monotonic() - started_at
        except Exception:
            duration_seconds = 0.0
        if lifecycle_service is None:
            lifecycle_service = create_lifecycle_service(project_root, runtime_root)
        finalized_usage = extracted.usage if extracted is not None else None
        durable_report_completion = extracted is not None and has_durable_report_completion(
            extracted.report.content
        )
        cancelled = not final_attempt_terminal_observed and (
            failure_reason in {"cancelled", "terminated"}
            or received_signal[0] in {signal.SIGINT, signal.SIGTERM}
        )
        status, exit_code, failure_reason = resolve_execution_terminal_state(
            exit_code=exit_code,
            failure_reason=failure_reason,
            cancelled=cancelled,
            durable_report_completion=durable_report_completion,
            terminated_after_completion=terminated_after_completion,
        )
        with signal_coordinator().mask_sigterm():
            spawn_service = SpawnApplicationService(
                runtime_root,
                lifecycle_service,
                spawn_manager=manager,
            )
            current_record = spawn_service.get_spawn(run.spawn_id)
            will_mark_finalizing = (
                current_record is not None and current_record.status != "finalizing"
            )
            finalized = await spawn_service.complete_spawn(
                run.spawn_id,
                status,
                exit_code,
                origin="runner",
                duration_secs=duration_seconds,
                total_cost_usd=(
                    finalized_usage.total_cost_usd if finalized_usage is not None else None
                ),
                input_tokens=finalized_usage.input_tokens if finalized_usage is not None else None,
                output_tokens=(
                    finalized_usage.output_tokens if finalized_usage is not None else None
                ),
                cache_read_input_tokens=(
                    finalized_usage.cache_read_input_tokens if finalized_usage is not None else None
                ),
                cache_creation_input_tokens=(
                    finalized_usage.cache_creation_input_tokens
                    if finalized_usage is not None
                    else None
                ),
                reasoning_tokens=(
                    finalized_usage.reasoning_tokens if finalized_usage is not None else None
                ),
                cost_is_estimate=(
                    finalized_usage.cost_is_estimate if finalized_usage is not None else False
                ),
                error=failure_reason,
            )
            if finalized and will_mark_finalizing:
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
            elif not finalized:
                logger.info(
                    "Runner finalize skipped; spawn already terminal or missing.",
                    spawn_id=str(run.spawn_id),
                    harness_id=str(launch_context.harness.id),
                )

    return exit_code


__all__ = [
    "DEFAULT_GUARDRAIL_TIMEOUT_SECONDS",
    "TerminalEventOutcome",
    "execute_with_streaming",
    "run_streaming_spawn",
    "terminal_event_outcome",
]
