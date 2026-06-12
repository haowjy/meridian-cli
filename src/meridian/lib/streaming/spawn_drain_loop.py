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
from meridian.lib.streaming.drain_coordinator import (
    DrainCoordinator,
    DrainExitDecision,
    DrainLoopDecision,
    DrainPlan,
    DrainTerminalDecision,
)
from meridian.lib.streaming.drain_policy import DrainAction
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
    from meridian.lib.harness.semantics import PrimaryEventScope, TerminalEventOutcome
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
        drain_plan: DrainPlan,
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

        coordinator = drain_plan.coordinator
        policy = drain_plan.selected_policy()
        if drain_plan.on_policy_selected is not None:
            drain_plan.on_policy_selected(policy)
        if coordinator is not None:
            await coordinator.start()

        events_iter = receiver.events().__aiter__()
        drain_waiter = DrainInputWaiter(events_iter, drain_plan.aux_wake or _NoAuxWake())
        try:
            while True:
                wake = await drain_waiter.wait(_next_timeout(coordinator))
                if isinstance(wake, DrainClosedWake):
                    session = self._sessions.get(spawn_id)
                    close_outcome = (
                        coordinator.handle_close(
                            intentional_stop=bool(session.cancel_sent)
                            if session is not None
                            else False,
                        )
                        if coordinator is not None
                        else None
                    )
                    if close_outcome is not None:
                        recorded_terminal_outcome = close_outcome
                    break
                if isinstance(wake, DrainAuxWake):
                    aux_wake_outcome = await _handle_aux_wake(drain_plan)
                    if aux_wake_outcome.recorded_outcome is not None:
                        recorded_terminal_outcome = aux_wake_outcome.recorded_outcome
                        break
                    continue
                if isinstance(wake, DrainTimeoutWake):
                    timeout_outcome = await _handle_timeout(coordinator)
                    if timeout_outcome.recorded_outcome is not None:
                        recorded_terminal_outcome = timeout_outcome.recorded_outcome
                        break
                    continue

                event = wake.event
                disk_change_ready_after_event = wake.disk_change_ready_after_event

                transition = activity_transition(
                    event,
                    primary_event_scope=_primary_event_scope(receiver),
                )
                duplicate_canonical_event = await _observe_event(
                    coordinator,
                    event,
                    transition,
                )
                if duplicate_canonical_event:
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
                    primary_event_scope=_primary_event_scope(receiver),
                )
                self._fan_out_event(spawn_id, event)
                persisted_event_decision = _note_event_persisted(coordinator, event)
                if persisted_event_decision.recorded_outcome is not None:
                    recorded_terminal_outcome = persisted_event_decision.recorded_outcome
                    break
                if disk_change_ready_after_event:
                    # Disk change arrived concurrently with this event; reevaluate now
                    # that the event has been persisted and observers notified.
                    aux_wake_outcome = await _handle_aux_wake(drain_plan)
                    if aux_wake_outcome.recorded_outcome is not None:
                        recorded_terminal_outcome = aux_wake_outcome.recorded_outcome
                        break

                if event_outcome is not None:
                    action = policy.classify(event_outcome)
                    terminal_decision = await _handle_terminal_event(
                        coordinator,
                        event,
                        event_outcome,
                        action,
                    )
                    if terminal_decision.recorded_outcome is not None:
                        recorded_terminal_outcome = terminal_decision.recorded_outcome
                        break
                    if terminal_decision.emit_turn_boundary:
                        await self._fan_out_turn_boundary(spawn_id, event_outcome)

                after_event_outcome = await _after_event(coordinator)
                if after_event_outcome.recorded_outcome is not None:
                    recorded_terminal_outcome = after_event_outcome.recorded_outcome
                    break
        except asyncio.CancelledError:
            drain_cancelled = True
            raise
        except Exception as exc:
            drain_error = exc
            raise
        finally:
            await drain_waiter.close()
            try:
                exit_decision = await _handle_stream_exit(coordinator, recorded_terminal_outcome)
            finally:
                if coordinator is not None:
                    await coordinator.stop()
            recorded_terminal_outcome = exit_decision.recorded_outcome
            session = self._sessions.pop(spawn_id, None)
            if session is not None:
                fallback_error = exit_decision.fallback_error
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
                        authoritative=(
                            not drain_plan.raw_terminal_frames_authoritative
                        ),
                    )
                else:
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error="connection_closed_without_terminal_event",
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                if drain_plan.finalizer is not None:
                    drain_plan.finalizer.after_finalized(
                        connection_session_id=_safe_connection_session_id(receiver),
                        outcome=outcome,
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


def _primary_event_scope(connection: object) -> PrimaryEventScope | None:
    """Read the connection's primary event scope when it exposes one."""

    try:
        scope = cast("Any", connection).primary_event_scope
    except Exception:
        return None
    return cast("PrimaryEventScope | None", scope)


class _NoAuxWake:
    """Waiter adapter for the plain single-turn baseline."""

    def wants_aux_wake(self) -> bool:
        return False

    async def wait_for_aux_wake(self) -> None:
        return


def _next_timeout(coordinator: DrainCoordinator | None) -> float | None:
    if coordinator is None:
        return None
    return coordinator.next_timeout()


async def _observe_event(
    coordinator: DrainCoordinator | None,
    event: HarnessEvent,
    transition: str | None,
) -> bool:
    if coordinator is None:
        return False
    return await coordinator.observe_event(event, transition)


def _note_event_persisted(
    coordinator: DrainCoordinator | None,
    event: HarnessEvent,
) -> DrainLoopDecision:
    if coordinator is None:
        return DrainLoopDecision()
    return coordinator.note_event_persisted(event)


async def _handle_terminal_event(
    coordinator: DrainCoordinator | None,
    event: HarnessEvent,
    outcome: TerminalEventOutcome,
    action: DrainAction,
) -> DrainTerminalDecision:
    if coordinator is None:
        return DrainTerminalDecision(
            recorded_outcome=outcome if action.terminate else None,
            emit_turn_boundary=action.emit_turn_boundary,
        )
    return await coordinator.handle_terminal_event(event, outcome, action)


async def _handle_aux_wake(drain_plan: DrainPlan) -> DrainLoopDecision:
    if drain_plan.handle_aux_wake is None:
        return DrainLoopDecision()
    return await drain_plan.handle_aux_wake()


async def _handle_timeout(coordinator: DrainCoordinator | None) -> DrainLoopDecision:
    if coordinator is None:
        return DrainLoopDecision()
    return await coordinator.handle_timeout()


async def _after_event(coordinator: DrainCoordinator | None) -> DrainLoopDecision:
    if coordinator is None:
        return DrainLoopDecision()
    return await coordinator.after_event()


async def _handle_stream_exit(
    coordinator: DrainCoordinator | None,
    recorded_outcome: TerminalEventOutcome | None,
) -> DrainExitDecision:
    if coordinator is None:
        return DrainExitDecision(recorded_outcome=recorded_outcome)
    return await coordinator.handle_stream_exit(recorded_outcome)


def _safe_connection_session_id(connection: object) -> str | None:
    """Read optional connection session_id without assuming full HarnessConnection shape."""

    try:
        session_id = cast("Any", connection).session_id
    except Exception:
        return None
    return session_id if isinstance(session_id, str) and session_id.strip() else None
