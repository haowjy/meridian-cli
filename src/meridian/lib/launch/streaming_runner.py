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
    persist_attempt_artifacts,
    preserve_attempt_artifacts,
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
    classify_error,
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
from meridian.lib.launch.retry import (
    AttemptFailure,
    ReplayEvidence,
    RetryPermit,
    classify_attempt_failure,
    decide_retry,
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
    AttemptRuntime,
    install_signal_handlers,
    run_streaming_attempt,
    run_streaming_spawn,
    touch_heartbeat_file,
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

    def commit_attempt(
        self,
        attempt: AttemptRuntime,
        *,
        exit_code: int,
        failure: AttemptFailure | None,
        terminal_status: TerminalSpawnStatus | None,
        cancelled: bool,
    ) -> None:
        """Select final reporting and retry cause from one typed record."""

        self.failure_reason = None if failure is None else failure.final_message
        self.exit_code = exit_code
        self.final_attempt_terminal_observed = attempt.terminal_observed
        self.authoritative_terminal_status = terminal_status
        self.cancellation_observed = self.cancellation_observed or cancelled

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
        lambda: touch_heartbeat_file(
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
        signal_cleanup = install_signal_handlers(
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
                runner_phase[0] = "starting_attempt"
                _record_lifecycle(
                    "attempt_started",
                    attempt=attempt_number,
                    startup_attempt_id=session_attempt.startup_attempt_id,
                )
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

                attempt = await run_streaming_attempt(
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
                attempt_exit_code = attempt.drain_exit_code
                attempt_terminal_status = attempt.authoritative_terminal_status
                attempt_failure_message = attempt.start_error
                if attempt.start_error is not None:
                    logger.info(
                        "Failed to execute streaming spawn attempt.",
                        spawn_id=str(run.spawn_id),
                        harness_id=str(harness.id),
                        error=attempt.start_error,
                    )
                    _append_text_to_stderr_artifact(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        text=attempt.start_error,
                    )
                attempt_cancelled = False
                cancellation_message: str | None = None
                if attempt.timed_out:
                    attempt_failure_message = "timeout"
                if not attempt.terminal_observed:
                    if attempt.received_signal == signal.SIGINT:
                        attempt_failure_message = "cancelled"
                        cancellation_message = "cancelled"
                        attempt_cancelled = True
                    elif attempt.received_signal == signal.SIGTERM:
                        attempt_failure_message = "terminated"
                        cancellation_message = "terminated"
                        attempt_cancelled = True
                if (
                    attempt_exit_code != 0
                    and attempt_failure_message is None
                    and attempt.drain_error is not None
                ):
                    attempt_failure_message = attempt.drain_error

                persist_attempt_artifacts(
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
                    teardown=attempt.teardown,
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
                    failure_reason=attempt_failure_message,
                )
                conclusion.extracted = extraction
                terminal_code: str | None = None
                terminal_message: str | None = None
                guardrail_failed = False
                budget_exceeded = False
                attempt_cancelled = attempt_cancelled or attempt.cancelled_by_request

                if outcome.error is not None:
                    attempt_exit_code = 1
                    attempt_failure_message = outcome.error.failure_code
                    attempt_terminal_status = "failed"
                    terminal_code = outcome.error.failure_code
                    terminal_message = outcome.error.failure_code

                cancel_intent = _read_cancel_intent(runtime_root, run.spawn_id)
                if cancel_intent is not None and not extraction.durable_report_completion:
                    attempt_exit_code = cancel_intent.exit_code
                    attempt_failure_message = cancel_intent.error or "cancelled"
                    cancellation_message = attempt_failure_message
                    attempt_cancelled = True

                if attempt_cancelled and attempt.received_signal is not None:
                    attempt_exit_code = signal_to_exit_code(attempt.received_signal) or 130

                if attempt.budget_breach is not None:
                    budget_exceeded = True
                    attempt_failure_message = "budget_exceeded"
                    attempt_exit_code = DEFAULT_INFRA_EXIT_CODE
                    _append_budget_exceeded_event(run=run, breach=attempt.budget_breach)

                if (
                    not budget_exceeded
                    and budget_tracker is not None
                    and extraction.usage is not None
                    and extraction.usage.total_cost_usd is not None
                    and budget_tracker.observe_cost(extraction.usage.total_cost_usd) is not None
                ):
                    budget_exceeded = True
                    attempt_failure_message = "budget_exceeded"
                    breach = budget_tracker.check()
                    if breach is not None:
                        _append_budget_exceeded_event(run=run, breach=breach)
                    attempt_exit_code = DEFAULT_INFRA_EXIT_CODE

                if attempt.terminated_by_inactivity:
                    exit_override, failure_override = _inactivity_terminal_outcome(extraction)
                    if exit_override is not None:
                        attempt_exit_code = exit_override
                    attempt_failure_message = failure_override
                    if failure_override is not None:
                        terminal_code = failure_override
                        terminal_message = failure_override

                if (
                    attempt_exit_code == 0
                    and terminal_code is None
                    and not attempt_cancelled
                    and not budget_exceeded
                    and _spawn_kind(runtime_root, run.spawn_id) == "child"
                    and extraction.report.content is None
                ):
                    attempt_failure_message = "missing_report"
                    attempt_exit_code = 1
                    terminal_code = "missing_report"
                    terminal_message = "missing_report"

                if attempt.terminated_by_report_watchdog and extraction.durable_report_completion:
                    attempt_exit_code = 0
                    attempt_failure_message = None

                if (
                    extraction.output_is_empty
                    and attempt_exit_code == 0
                    and not attempt.terminated_by_report_watchdog
                ):
                    attempt_exit_code = 1
                    attempt_failure_message = "empty_output"
                    terminal_code = "empty_output"
                    terminal_message = "empty_output"

                if (
                    attempt_exit_code == 0
                    and terminal_code is None
                    and not attempt_cancelled
                    and not budget_exceeded
                ):
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
                        conclusion.commit_attempt(
                            attempt,
                            exit_code=attempt_exit_code,
                            failure=None,
                            terminal_status=attempt_terminal_status,
                            cancelled=attempt_cancelled,
                        )
                        break
                    guardrail_failed = True
                    attempt_exit_code = 1
                    attempt_failure_message = "guardrail_failed"
                    _append_text_to_stderr_artifact(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        text=_guardrail_failure_text(guardrail_result.failures),
                    )

                stderr_key = make_artifact_key(run.spawn_id, STDERR_FILENAME)
                stderr_text = (
                    artifacts.get(stderr_key).decode("utf-8", errors="ignore")
                    if artifacts.exists(stderr_key)
                    else ""
                )
                category = classify_error(
                    attempt_exit_code,
                    stderr_text,
                    timed_out=attempt.timed_out,
                    failure_message=attempt.drain_error,
                )
                resolved_terminal_code = terminal_code or (
                    f"authoritative_{attempt_terminal_status}"
                    if attempt_terminal_status is not None
                    else None
                )
                resolved_terminal_message = terminal_message
                if resolved_terminal_message is None and resolved_terminal_code is not None:
                    resolved_terminal_message = attempt.drain_error or resolved_terminal_code
                failure = classify_attempt_failure(
                    cancelled=attempt_cancelled or conclusion.cancellation_observed,
                    cancellation_message=cancellation_message,
                    terminal_outcome=attempt.terminal_outcome,
                    terminal_code=resolved_terminal_code,
                    terminal_message=resolved_terminal_message,
                    start_failure=attempt.start_failure,
                    guardrail_failed=guardrail_failed,
                    timed_out=attempt.timed_out,
                    budget_exceeded=budget_exceeded,
                    legacy_category=category,
                    fallback_message=attempt_failure_message or attempt.drain_error,
                )
                conclusion.commit_attempt(
                    attempt,
                    exit_code=attempt_exit_code,
                    failure=failure,
                    terminal_status=attempt_terminal_status,
                    cancelled=attempt_cancelled,
                )
                decision = decide_retry(
                    failure,
                    ReplayEvidence(
                        attempt.turn_submission,
                        outcome.native_create,
                        attempt.teardown,
                    ),
                    attempts_used=attempt_number,
                    max_attempts=request.retry.max_attempts,
                )
                assessment = decision.assessment
                _record_lifecycle(
                    "retry_assessed",
                    attempt=attempt_number,
                    startup_attempt_id=session_attempt.startup_attempt_id,
                    planned_native_operation=(
                        native_run.identity.operation if native_run.identity is not None else None
                    ),
                    planned_native_id=native_run.assigned_session_id,
                    failure_disposition=assessment.failure.disposition.value,
                    failure_code=assessment.failure.code,
                    turn_submission=assessment.evidence.turn.value,
                    native_create=assessment.evidence.native_create.value,
                    teardown=assessment.evidence.teardown.value,
                    replay_safety=assessment.replay_safety.value,
                    retry=decision.retry,
                    reason=assessment.reason,
                )
                if not decision.retry:
                    break

                permit = RetryPermit(decision)
                next_session_attempt = replace(
                    session_attempt,
                    startup_attempt_id=uuid.uuid4().hex,
                )
                try:
                    next_native_run = native_run.rearm(next_session_attempt, permit)
                    preserve_attempt_artifacts(
                        artifacts=artifacts,
                        spawn_id=run.spawn_id,
                        log_dir=log_dir,
                        completed_attempt=attempt_number,
                    )
                except Exception as exc:
                    # Diagnostics are subordinate to the causal attempt. A failure
                    # while recording retry setup must not replace that outcome.
                    with suppress(Exception):
                        _record_lifecycle(
                            "retry_setup_failed",
                            attempt=attempt_number,
                            exception_type=type(exc).__name__,
                            exception=str(exc),
                        )
                    with suppress(Exception):
                        _append_text_to_stderr_artifact(
                            artifacts=artifacts,
                            spawn_id=run.spawn_id,
                            text=f"retry setup failed: {exc}",
                        )
                    break

                conclusion.retries_attempted += 1
                session_attempt = next_session_attempt
                native_run = next_native_run
                config = replace(config, session_id_observer=native_run.observe)
                logger.info(
                    "Retrying proven-safe startup failure.",
                    spawn_id=str(run.spawn_id),
                    harness_id=str(harness.id),
                    retries_attempted=conclusion.retries_attempted,
                    max_attempts=request.retry.max_attempts,
                    failure_code=failure.code,
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
