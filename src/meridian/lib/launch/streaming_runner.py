"""Bidirectional spawn execution with lifecycle-owned terminal finalization."""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from meridian.lib.bootstrap.services import (
    build_spawn_application_service_from_roots,
    build_spawn_lifecycle_service_from_roots,
)
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.clock import Clock, RealClock
from meridian.lib.core.domain import Spawn, SpawnStatus, TerminalSpawnStatus
from meridian.lib.core.native_identity import (
    NativeIdentityError,
)
from meridian.lib.core.spawn_lifecycle import ExecutionTerminalFacts
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import StreamEvent
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection
from meridian.lib.harness.extractors.base import AttemptFold
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.artifact_io import LifecycleLog, record_identity_failure
from meridian.lib.launch.artifact_io import (
    append_runner_lifecycle_event as _append_runner_lifecycle_event,
)
from meridian.lib.launch.attempt_artifacts import (
    _persist_attempt_artifacts,
    _preserve_attempt_artifacts,
)
from meridian.lib.launch.constants import (
    DEFAULT_INFRA_EXIT_CODE,
    STDERR_FILENAME,
)
from meridian.lib.launch.context import LaunchContext
from meridian.lib.launch.env import (
    apply_pi_bind_time_env,
    resolve_pi_session_role,
)
from meridian.lib.launch.errors import (
    ErrorCategory,
    classify_error,
    should_retry,
)
from meridian.lib.launch.extract import (
    FinalizeExtraction,
    enrich_finalize,
    reset_finalize_attempt_artifacts,
)
from meridian.lib.launch.native_run import bind_entry, conclude_native_run
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
    guardrail_failure_text as _guardrail_failure_text,
)
from meridian.lib.launch.runner_helpers import (
    spawn_kind as _spawn_kind,
)
from meridian.lib.launch.session_scope import SessionAttempt
from meridian.lib.launch.signals import signal_coordinator, signal_to_exit_code
from meridian.lib.launch.streaming.attempt import (
    _AttemptRuntime,
    _install_signal_handlers,
    _run_streaming_attempt,
    _touch_heartbeat_file,
    run_streaming_spawn,
)
from meridian.lib.launch.streaming.heartbeat import HeartbeatTouch
from meridian.lib.safety.budget import Budget, LiveBudgetTracker
from meridian.lib.safety.guardrails import run_guardrails
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import ArtifactStore, make_artifact_key
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.spawn.model import (
    BACKGROUND_LAUNCH_MODE,
    FOREGROUND_LAUNCH_MODE,
    LaunchMode,
)
from meridian.lib.streaming.spawn_manager import SpawnManager
from meridian.lib.utils.time import minutes_to_seconds

if TYPE_CHECKING:
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.state.spawn.model import CancelIntent

_DEFAULT_CONFIG = MeridianConfig()
DEFAULT_GUARDRAIL_TIMEOUT_SECONDS = _DEFAULT_CONFIG.guardrail_timeout_minutes * 60.0
logger = structlog.get_logger(__name__)
_HEARTBEAT_INTERVAL_SECS = 30.0




@dataclass
class StreamingRunConclusion:
    """Accumulates execution outcome across retry attempts."""

    exit_code: int = DEFAULT_INFRA_EXIT_CODE
    failure_reason: str | None = None
    extracted: FinalizeExtraction | None = None
    final_attempt_terminal_observed: bool = False
    authoritative_terminal_status: TerminalSpawnStatus | None = None
    cancellation_observed: bool = False
    retries_attempted: int = 0

    def absorb_attempt(self, attempt: _AttemptRuntime) -> None:
        """Merge one attempt's terminal fields into the run conclusion."""

        self.failure_reason = None
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






def _retry_blocked_after_pi_child_started(
    *, harness_id: HarnessId, runtime_root: Path, current_spawn_id: SpawnId
) -> bool:
    """Return whether retrying would orphan already-started Pi child spawn work."""

    if harness_id is not HarnessId.PI:
        return False
    scan = spawn_store.list_spawns(runtime_root, parent_id=str(current_spawn_id))
    # Retry policy fails closed: an unreadable sibling could be a child whose
    # already-started work must not be orphaned by a new attempt.
    return bool(
        scan.records
        or any(spawn_store.is_spawn_id_shape(report.spawn_id) for report in scan.quarantines)
    )


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
    harness_session_id_observer: Callable[[str], None] | None = None,
    session_attempt: SessionAttempt,
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
    lifecycle: LifecycleLog | None = None
    lifecycle_active = [False]
    atexit_callback: Callable[[], None] | None = None

    try:
        log_dir = resolve_spawn_log_dir(project_root, run.spawn_id, runtime_root=runtime_root)
        lifecycle = LifecycleLog.for_spawn(
            runtime_root, project_root, run.spawn_id, clock=resolved_clock
        )
        lifecycle_path = lifecycle.path

        def _record_lifecycle(event: str, **details: object) -> None:
            assert lifecycle_path is not None
            _append_runner_lifecycle_event(
                runtime_root,
                run.spawn_id,
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

        native_run = bind_entry(
            session_attempt,
            spec,
            harness=str(resolved_harness_id),
            on_accepted=harness_session_id_observer,
        )
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
            session_id_observer=native_run.observe,
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

                fold = harness_bundle.extractor.create_fold()
                facts = fold.facts
                attempt_number = conclusion.retries_attempted + 1
                if attempt_number > 1:
                    session_attempt = replace(
                        session_attempt,
                        startup_attempt_id=uuid.uuid4().hex,
                    )
                    native_run = native_run.retry(session_attempt)
                    config = replace(config, session_id_observer=native_run.observe)
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

                attempt_pid: int | None = None

                def record_started(
                    connection: HarnessConnection[Any],
                    captured_observer: Callable[[str], None] = native_run.observe,
                    attempt_fold: AttemptFold = fold,
                ) -> None:
                    nonlocal attempt_pid
                    attempt_pid = connection.subprocess_pid
                    attempt_fold.bind_scope(connection.session_id)
                    native_id = connection.session_id
                    if native_id:
                        captured_observer(native_id)

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
                    on_running=record_started,
                    event_hook=fold,
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
                )

                outcome = conclude_native_run(
                    native_run,
                    harness,
                    context=launch_context,
                    spawn_id=run.spawn_id,
                    child_env=child_env,
                    child_cwd=child_cwd,
                    pid=attempt_pid,
                    started=attempt_pid is not None,
                    started_at_epoch=started_at_epoch,
                    prior_error=attempt.identity_error,
                    prior_error_phase="running",
                    facts=facts,
                    connection_session_id=(
                        attempt.connection.session_id if attempt.connection is not None else None
                    ),
                    lifecycle=lifecycle,
                )
                extraction = enrich_finalize(
                    artifacts=artifacts,
                    extractor=harness_bundle.extractor,
                    facts=facts,
                    native_key=native_run.entry.complete(),
                    spawn_id=run.spawn_id,
                    log_dir=log_dir,
                    model_id=run.model,
                    harness_id=resolved_harness_id,
                    project_root=project_root,
                    failure_reason=conclusion.failure_reason,
                )
                conclusion.extracted = extraction
                if outcome.error is not None:
                    conclusion.exit_code = 1
                    conclusion.failure_reason = outcome.error.failure_code
                    conclusion.authoritative_terminal_status = "failed"
                    break

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
                    and extraction.usage is not None
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
                    exit_override, failure_override = _inactivity_terminal_outcome(extraction)
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

                if extraction.output_is_empty and conclusion.exit_code == 0:
                    conclusion.exit_code = 1
                    conclusion.failure_reason = "empty_output"
                    break
                if conclusion.exit_code == 0:
                    guardrail_spawn = spawn_store.get_spawn(runtime_root, run.spawn_id)
                    guardrail_result = run_guardrails(
                        guardrails,
                        spawn_id=run.spawn_id,
                        cwd=child_cwd,
                        env=child_env,
                        report_path=extraction.report_path,
                        chat_id=(guardrail_spawn.continue_chat_id if guardrail_spawn else None),
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
    except NativeIdentityError as exc:
        conclusion.exit_code = 1
        conclusion.failure_reason = exc.failure_code
        if lifecycle is not None:
            record_identity_failure(exc, lifecycle=lifecycle, phase=runner_phase[0])
    except Exception as exc:
        if lifecycle_path is not None:
            _append_runner_lifecycle_event(
                runtime_root,
                run.spawn_id,
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
            "Streaming spawn failed.",
            spawn_id=str(run.spawn_id),
            harness_id=str(launch_context.harness.id),
        )
    finally:
        runner_phase[0] = "finalizing"
        if lifecycle_path is not None:
            _append_runner_lifecycle_event(
                runtime_root,
                run.spawn_id,
                lifecycle_path,
                clock=resolved_clock,
                event="finalizing",
                phase=runner_phase[0],
            )
        if signal_cleanup is not None:
            signal_cleanup()
        if manager is not None:
            with suppress(Exception):
                await manager.shutdown(status=SpawnStatus.CANCELLED, exit_code=1, error="shutdown")
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
                runtime_root,
                run.spawn_id,
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
