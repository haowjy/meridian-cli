"""Durable event drain loop for one streaming spawn."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.state.history import HarnessHistoryWriter
from meridian.lib.streaming.drain_coordinator import DrainCoordinator
from meridian.lib.streaming.drain_policy import DrainPolicy
from meridian.lib.streaming.drain_wait import (
    DrainAuxWake,
    DrainClosedWake,
    DrainInputWaiter,
    DrainTimeoutWake,
)
from meridian.lib.streaming.event_observers import EventObserverRegistry
from meridian.lib.streaming.spawn_session import DrainOutcome, SpawnSession

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.observability.debug_tracer import DebugTracer

logger = logging.getLogger(__name__)

CleanupCompletedSession = Callable[[SpawnId, SpawnSession], Coroutine[Any, Any, None]]
ResolveCompletionFuture = Callable[[SpawnSession, DrainOutcome], DrainOutcome]
FanOutEvent = Callable[[SpawnId, HarnessEvent], None]
FanOutTurnBoundary = Callable[[SpawnId, "TerminalEventOutcome"], Awaitable[None]]


class SpawnDrainLoop:
    """Run the per-spawn event drain and completion finalization."""

    def __init__(
        self,
        *,
        sessions: dict[SpawnId, SpawnSession],
        history_writers: dict[SpawnId, HarnessHistoryWriter],
        observers: EventObserverRegistry,
        cleanup_tasks: set[asyncio.Task[None]],
        cleanup_completed_session: CleanupCompletedSession,
        resolve_completion_future: ResolveCompletionFuture,
        fan_out_event: FanOutEvent,
        fan_out_turn_boundary: FanOutTurnBoundary,
    ) -> None:
        self._sessions = sessions
        self._history_writers = history_writers
        self._observers = observers
        self._cleanup_tasks = cleanup_tasks
        self._cleanup_completed_session = cleanup_completed_session
        self._resolve_completion_future = resolve_completion_future
        self._fan_out_event = fan_out_event
        self._fan_out_turn_boundary = fan_out_turn_boundary

    async def run(
        self,
        *,
        spawn_id: SpawnId,
        receiver: HarnessConnection[Any],
        coordinator: DrainCoordinator,
        drain_policy: DrainPolicy | None,
        tracer: DebugTracer | None,
    ) -> None:
        """Durably append each harness event and fan out to the active subscriber."""

        # Import at runtime to avoid circular import during module initialization.
        from meridian.lib.harness.semantics import activity_transition, terminal_outcome

        consecutive_write_failures = 0
        max_consecutive_failures = 10
        drain_cancelled = False
        drain_error: Exception | None = None
        recorded_terminal_outcome: TerminalEventOutcome | None = None

        policy = drain_policy or coordinator.default_policy()
        coordinator.set_policy(policy)
        await coordinator.start()

        events_iter = receiver.events().__aiter__()
        drain_waiter = DrainInputWaiter(events_iter, coordinator)
        try:
            while True:
                wake = await drain_waiter.wait(coordinator.next_timeout())
                if isinstance(wake, DrainClosedWake):
                    session = self._sessions.get(spawn_id)
                    close_outcome = coordinator.handle_close(
                        intentional_stop=bool(session.cancel_sent)
                        if session is not None
                        else False,
                    )
                    if close_outcome is not None:
                        recorded_terminal_outcome = close_outcome
                    break
                if isinstance(wake, DrainAuxWake):
                    aux_wake_outcome = await coordinator.handle_aux_wake()
                    if aux_wake_outcome.recorded_outcome is not None:
                        recorded_terminal_outcome = aux_wake_outcome.recorded_outcome
                        break
                    continue
                if isinstance(wake, DrainTimeoutWake):
                    timeout_outcome = await coordinator.handle_timeout()
                    if timeout_outcome.recorded_outcome is not None:
                        recorded_terminal_outcome = timeout_outcome.recorded_outcome
                        break
                    continue

                event = wake.event
                disk_change_ready_after_event = wake.disk_change_ready_after_event

                transition = activity_transition(
                    event,
                    codex_main_thread_id=_codex_main_thread_id(receiver),
                )
                duplicate_canonical_lifecycle_event = await coordinator.observe_event(
                    event,
                    transition,
                )
                if duplicate_canonical_lifecycle_event:
                    continue

                if tracer is not None:
                    tracer.emit(
                        "drain",
                        "event_received",
                        direction="inbound",
                        data={"event_type": event.event_type, "harness_id": event.harness_id},
                    )
                history_writer = self._history_writers.get(spawn_id)
                if history_writer is not None:
                    try:
                        write_result = history_writer.write(event)
                        if not write_result.success:
                            raise RuntimeError(write_result.error or "history write failed")
                        consecutive_write_failures = 0
                        if tracer is not None:
                            tracer.emit(
                                "drain",
                                "event_persisted",
                                data={"event_type": event.event_type},
                            )
                        self._observers.dispatch(spawn_id, event)
                    except Exception as persist_exc:
                        consecutive_write_failures += 1
                        if tracer is not None:
                            tracer.emit(
                                "drain",
                                "persist_error",
                                data={
                                    "event_type": event.event_type,
                                    "error": str(persist_exc),
                                    "consecutive_failures": consecutive_write_failures,
                                },
                            )
                        logger.warning(
                            "Failed to persist event for spawn %s (%d/%d consecutive failures)",
                            spawn_id,
                            consecutive_write_failures,
                            max_consecutive_failures,
                            exc_info=True,
                        )
                        if consecutive_write_failures >= max_consecutive_failures:
                            logger.error(
                                (
                                    "Aborting drain loop for spawn %s after %d "
                                    "consecutive write failures"
                                ),
                                spawn_id,
                                max_consecutive_failures,
                            )
                            drain_error = RuntimeError(
                                "Aborted drain loop after repeated output persistence failures"
                            )
                            self._fan_out_event(spawn_id, event)
                            break

                event_outcome = terminal_outcome(
                    event,
                    codex_main_thread_id=_codex_main_thread_id(receiver),
                )
                self._fan_out_event(spawn_id, event)
                coordinator.note_event_persisted(event)
                if disk_change_ready_after_event:
                    # Disk change arrived concurrently with this event; reevaluate now
                    # that the event has been persisted and observers notified.
                    aux_wake_outcome = await coordinator.handle_aux_wake()
                    if aux_wake_outcome.recorded_outcome is not None:
                        recorded_terminal_outcome = aux_wake_outcome.recorded_outcome
                        break

                pi_lifecycle_error = coordinator.lifecycle_error_outcome()
                if pi_lifecycle_error is not None:
                    recorded_terminal_outcome = pi_lifecycle_error
                    break

                if event_outcome is not None:
                    action = policy.classify(event_outcome)
                    terminal_decision = await coordinator.handle_terminal_event(
                        event,
                        event_outcome,
                        action,
                    )
                    if terminal_decision.recorded_outcome is not None:
                        recorded_terminal_outcome = terminal_decision.recorded_outcome
                        break
                    if terminal_decision.emit_turn_boundary:
                        await self._fan_out_turn_boundary(spawn_id, event_outcome)

                after_event_outcome = await coordinator.after_event()
                if after_event_outcome.recorded_outcome is not None:
                    recorded_terminal_outcome = after_event_outcome.recorded_outcome
                    break

                pi_failure = coordinator.failure_outcome_after_event()
                if pi_failure is not None:
                    recorded_terminal_outcome = pi_failure
                    break
                if recorded_terminal_outcome is None:
                    coordinator.maybe_start_quiescence_after_event()
        except asyncio.CancelledError:
            drain_cancelled = True
            raise
        except Exception as exc:
            drain_error = exc
            raise
        finally:
            pending_children_at_exit = coordinator.pending_children_at_exit()
            await drain_waiter.close()
            await coordinator.stop()
            await coordinator.cleanup_pending_children_at_exit()
            session = self._sessions.pop(spawn_id, None)
            if session is not None:
                fallback_error = coordinator.fallback_error_without_recorded_outcome()
                if drain_cancelled:
                    outcome = DrainOutcome(
                        status="cancelled",
                        exit_code=1,
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif drain_error is not None:
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error=str(drain_error),
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif session.cancel_sent:
                    outcome = DrainOutcome(
                        status="cancelled",
                        exit_code=143,
                        error="cancelled",
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif recorded_terminal_outcome is None and pending_children_at_exit:
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error="pi_process_exited_with_tracked_children",
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif fallback_error is not None and recorded_terminal_outcome is None:
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error=fallback_error,
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif recorded_terminal_outcome is not None:
                    outcome = DrainOutcome(
                        status=recorded_terminal_outcome.status,
                        exit_code=recorded_terminal_outcome.exit_code,
                        error=recorded_terminal_outcome.error,
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                else:
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error="connection_closed_without_terminal_event",
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                session_id = coordinator.finalization_session_id(
                    _safe_connection_session_id(receiver)
                )
                coordinator.emit_session_phase_if_needed(session_id)
                coordinator.emit_finalized(
                    status=outcome.status,
                    exit_code=outcome.exit_code,
                    error=outcome.error,
                )
                self._resolve_completion_future(session, outcome)
                cleanup_task = asyncio.create_task(
                    self._cleanup_completed_session(spawn_id, session)
                )
                self._cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(self._cleanup_tasks.discard)
            self._observers.complete(spawn_id)
            if session is not None and session.subscriber is not None:
                while True:
                    try:
                        session.subscriber.put_nowait(None)
                        break
                    except asyncio.QueueFull:
                        with suppress(asyncio.QueueEmpty):
                            session.subscriber.get_nowait()
                        continue


def _codex_main_thread_id(connection: object) -> str | None:
    """Read the tracked main Codex thread id when the connection exposes it."""

    try:
        thread_id = cast("Any", connection).main_turn_thread_id
    except Exception:
        return None
    return thread_id if isinstance(thread_id, str) and thread_id.strip() else None


def _safe_connection_session_id(connection: object) -> str | None:
    """Read optional connection session_id without assuming full HarnessConnection shape."""

    try:
        session_id = cast("Any", connection).session_id
    except Exception:
        return None
    return session_id if isinstance(session_id, str) and session_id.strip() else None
