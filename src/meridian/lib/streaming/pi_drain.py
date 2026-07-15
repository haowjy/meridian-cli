"""Pi-specific completion coordinator for streaming drains.

The generic spawn manager owns connection/event persistence. This module owns the
Pi policy layered on top: child/background work tracking, notification waits,
child-wave timeout, and quiescence micro-drain.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.state.spawn_signals import consume_resident_signals
from meridian.lib.streaming.completion_nudge import (
    COMPLETION_NUDGE_INTERVAL_SECONDS,
    PI_COMPLETION_NUDGE_MESSAGE,
)
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)
from meridian.lib.streaming.drain_policy import (
    DrainAction,
    DrainPolicy,
    PiRpcQuiescenceDrainPolicy,
)
from meridian.lib.streaming.pi_quiescence import PiQuiescenceTracker
from meridian.lib.streaming.pi_subspawn_tracker import PiPendingNotification, PiSubspawnTracker

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.launch.launch_types import ResolvedLaunchSpec
    from meridian.lib.streaming.spawn_session import DrainOutcome

_PI_PHASE_EVENT_TYPE = pi_lifecycle.PI_PHASE_EVENT_TYPE
logger = logging.getLogger(__name__)

PI_MICRO_DRAIN_TIMEOUT_SECONDS: float = 0.05
PI_DONE_NUDGE_IDLE_DELAY_SECONDS: float = 5.0
# Non-zero floor to prevent tight loops on expired deadlines.
_PI_TIMEOUT_FLOOR_SECONDS: float = 0.001

EmitPiPhase = Callable[..., None]
TerminatePiChildren = Callable[["PiSubspawnTracker", str], Awaitable[None]]
SendPiDoneNudge = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class PiOutstandingWork:
    """Classify Pi work that remains after the parent goes idle."""

    spawn_children: bool
    non_spawn_processes: bool
    unknown_spawn_children: bool = False


@dataclass
class PiDrainCoordinator:
    # TODO(phase-4): Pi quiescence is the Pi-specific instance of the same
    # resident-until-done model. Fold this into ResidentDrainCoordinator after
    # the Pi inference machinery is ripped out.
    runtime_root: Path
    spawn_id: SpawnId
    receiver: HarnessConnection[ResolvedLaunchSpec]
    session_role: str
    notification_timeout_seconds: float | None
    child_wave_timeout_seconds: float | None
    emit_phase: EmitPiPhase
    tracker: PiSubspawnTracker
    quiescence_tracker: PiQuiescenceTracker
    quiescence_enabled: bool = False
    last_successful_terminal: TerminalEventOutcome | None = None
    quiescence_candidate: TerminalEventOutcome | None = None
    micro_drain_event_count: int = 0
    session_seen: bool = False
    session_phase_emitted: bool = False
    waiting_child_count: int | None = None
    waiting_notification_count: int | None = None
    child_wave_deadline_monotonic: float | None = None
    child_wave_started_monotonic: float | None = None
    tracked_cleanup_reason: str | None = None
    tracked_cleanup_error: str | None = None
    terminate_children: TerminatePiChildren | None = None
    send_done_nudge: SendPiDoneNudge | None = None
    done_nudge_idle_delay_seconds: float = PI_DONE_NUDGE_IDLE_DELAY_SECONDS
    done_nudge_interval_seconds: float = COMPLETION_NUDGE_INTERVAL_SECONDS
    next_done_nudge_monotonic: float | None = None

    @classmethod
    def for_connection(
        cls,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        receiver: HarnessConnection[ResolvedLaunchSpec],
        session_role: str | None,
        notification_timeout_seconds: float | None,
        child_wave_timeout_seconds: float | None,
        emit_phase: EmitPiPhase,
        terminate_children: TerminatePiChildren | None = None,
        send_done_nudge: SendPiDoneNudge | None = None,
    ) -> PiDrainCoordinator:
        normalized_role = (session_role or "").strip().lower()
        return cls(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            receiver=receiver,
            session_role=normalized_role,
            notification_timeout_seconds=notification_timeout_seconds,
            child_wave_timeout_seconds=child_wave_timeout_seconds,
            emit_phase=emit_phase,
            tracker=PiSubspawnTracker.empty(),
            quiescence_tracker=PiQuiescenceTracker.for_connection(
                runtime_root=runtime_root,
                spawn_id=spawn_id,
                is_pi_connection=True,
                session_role=normalized_role,
            ),
            terminate_children=terminate_children,
            send_done_nudge=send_done_nudge,
        )

    async def start(self) -> None:
        await self.quiescence_tracker.start()
        self._emit("drain_started")

    async def stop(self) -> None:
        await self.quiescence_tracker.stop()
        self._clear_done_nudge_timer()

    def set_policy(self, policy: DrainPolicy) -> None:
        self.quiescence_enabled = isinstance(policy, PiRpcQuiescenceDrainPolicy)

    def next_timeout(self) -> float | None:
        if not self.quiescence_enabled:
            return None
        if self.quiescence_candidate is not None:
            return PI_MICRO_DRAIN_TIMEOUT_SECONDS

        now_monotonic = time.monotonic()
        next_timeout = self.tracker.time_until_next_notification_timeout(now_monotonic)
        if next_timeout is not None and next_timeout <= 0:
            next_timeout = _PI_TIMEOUT_FLOOR_SECONDS

        child_wave_remaining = self._child_wave_remaining(now_monotonic)
        if child_wave_remaining is not None:
            next_timeout = _min_timeout(next_timeout, child_wave_remaining)

        done_nudge_remaining = self._done_nudge_remaining(now_monotonic)
        if done_nudge_remaining is not None:
            next_timeout = _min_timeout(next_timeout, done_nudge_remaining)

        return next_timeout

    async def handle_timeout(
        self,
        terminate_children: TerminatePiChildren | None = None,
    ) -> DrainLoopDecision:
        terminate_children = terminate_children or self.terminate_children
        if not self.quiescence_enabled:
            raise TimeoutError
        if self.quiescence_candidate is not None:
            await self.quiescence_tracker.refresh_disk_state()
            if self.is_quiescent():
                return DrainLoopDecision(self.quiescence_candidate)
            self._emit(
                "quiescence_micro_drain_cancelled",
                reason="disk_state_changed",
            )
            self.quiescence_candidate = None
            self._update_idle_waiting_state()
            return DrainLoopDecision()

        now_monotonic = time.monotonic()
        done_decision = self._consume_done_signal()
        if done_decision is not None:
            return done_decision

        expired_notification = self.tracker.pop_expired_notification(now_monotonic)
        if expired_notification is not None:
            return DrainLoopDecision(
                self._notification_timeout_outcome(expired_notification, now_monotonic)
            )

        if self._child_wave_timed_out(now_monotonic):
            if terminate_children is None:
                raise RuntimeError("Pi child timeout cleanup is not configured")
            await self._handle_child_wave_timeout(terminate_children, now_monotonic)
            return DrainLoopDecision(
                _terminal_outcome(
                    status="failed",
                    exit_code=1,
                    error=_pi_child_wave_timeout_error(),
                )
            )

        if self._done_nudge_due(now_monotonic):
            self.next_done_nudge_monotonic = now_monotonic + self.done_nudge_interval_seconds
            await self._send_done_nudge()
            return DrainLoopDecision()

        return DrainLoopDecision()

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        duplicate = self.tracker.observe(
            event,
            now_monotonic=time.monotonic(),
            notification_timeout_seconds=self.notification_timeout_seconds,
        )
        if duplicate:
            return True
        if event.event_type == "session":
            self.session_seen = True
        if event.event_type == _PI_PHASE_EVENT_TYPE:
            phase_value = event.payload.get("phase")
            if phase_value in {"session_event_seen", "session_event_absent"}:
                self.session_phase_emitted = True
                if phase_value == "session_event_seen":
                    self.session_seen = True

        if transition == "turn_active":
            self.quiescence_tracker.mark_turn_active()
            self._clear_child_wave_timer()
        elif transition == "idle":
            await self.quiescence_tracker.mark_idle()
            self._update_idle_waiting_state()

        if not self.has_pending_children():
            self._clear_child_wave_timer()
        self._refresh_done_nudge_state()
        self.emit_waiting_phases_if_needed()
        return False

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        if self.quiescence_enabled and self.quiescence_candidate is not None:
            self.micro_drain_event_count += 1
            self.quiescence_candidate = None
            self._emit(
                "quiescence_micro_drain_extended",
                event_type=event.event_type,
                micro_drain_events=self.micro_drain_event_count,
            )
        lifecycle_error = self.lifecycle_error_outcome()
        if lifecycle_error is not None:
            return DrainLoopDecision(lifecycle_error)
        return DrainLoopDecision()

    def lifecycle_error_outcome(self) -> TerminalEventOutcome | None:
        if self.tracker.lifecycle_tracking_invalidated_error is None:
            return None
        return _terminal_outcome(
            status="failed",
            exit_code=1,
            error=self.tracker.lifecycle_tracking_invalidated_error,
        )

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        if outcome.status == "succeeded":
            self.last_successful_terminal = outcome
            completed_notification_id = self.tracker.resolve_notification_on_terminal(event)
            if completed_notification_id is not None:
                self._emit("continuation_completed", notification_id=completed_notification_id)
            self._refresh_done_nudge_state()
            self.emit_waiting_phases_if_needed()
        if action.terminate:
            if self.quiescence_enabled and outcome.status == "succeeded":
                if self.is_quiescent():
                    self.start_micro_drain(outcome)
                else:
                    self._emit(
                        "quiescence_deferred",
                        active_tracked_count=self.pending_child_count(),
                        pending_notification_count=self.tracker.pending_notification_count(),
                    )
                return DrainTerminalDecision()
            return DrainTerminalDecision(recorded_outcome=outcome)
        return DrainTerminalDecision(emit_turn_boundary=action.emit_turn_boundary)

    def failure_outcome_after_event(self) -> TerminalEventOutcome | None:
        if (
            self.tracker.notification_failure_error is not None
            and self.quiescence_tracker.parent_idle
            and not self.has_pending_children()
        ):
            return _terminal_outcome(
                status="failed",
                exit_code=1,
                error=self.tracker.notification_failure_error,
            )
        if self.tracker.notification_timeout_error is not None:
            return _terminal_outcome(
                status="failed",
                exit_code=1,
                error=self.tracker.notification_timeout_error,
            )
        return None

    def maybe_start_quiescence_after_event(self) -> None:
        if (
            self.quiescence_enabled
            and self.quiescence_candidate is None
            and self.last_successful_terminal is not None
            and self.is_quiescent()
        ):
            self.start_micro_drain(self.last_successful_terminal)
            self._clear_done_nudge_timer()

    def wants_aux_wake(self) -> bool:
        return self.quiescence_enabled

    async def wait_for_aux_wake(self) -> None:
        await self.quiescence_tracker.wait_for_disk_change()

    async def handle_aux_wake(self) -> DrainLoopDecision:
        await self.reevaluate_after_disk_change()
        return DrainLoopDecision()

    async def after_event(self) -> DrainLoopDecision:
        failure = self.failure_outcome_after_event()
        if failure is not None:
            return DrainLoopDecision(failure)
        self.maybe_start_quiescence_after_event()
        return DrainLoopDecision()

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None:
        return self.quiescence_candidate

    async def reevaluate_after_disk_change(self) -> None:
        if not self.quiescence_enabled:
            return
        await self.quiescence_tracker.refresh_disk_state()
        if not self.quiescence_tracker.parent_idle:
            return
        self._update_idle_waiting_state()
        self.maybe_start_quiescence_after_event()

    def _update_idle_waiting_state(self) -> None:
        if (
            self.child_wave_timeout_seconds is not None
            and self.child_wave_timeout_seconds > 0
            and self.has_pending_children()
            and self.child_wave_deadline_monotonic is None
        ):
            wave_start = time.monotonic()
            self.child_wave_started_monotonic = wave_start
            self.child_wave_deadline_monotonic = wave_start + self.child_wave_timeout_seconds
        if not self.has_pending_children():
            self._clear_child_wave_timer()
        self._refresh_done_nudge_state()
        self.emit_waiting_phases_if_needed()

    def pending_children_at_exit(self) -> bool:
        return self.quiescence_enabled and self.has_pending_children()

    async def cleanup_pending_children_at_exit(
        self,
        terminate_children: TerminatePiChildren | None = None,
    ) -> None:
        terminate_children = terminate_children or self.terminate_children
        if (
            self.tracker.has_pending()
            and self.tracked_cleanup_reason is None
        ):
            if terminate_children is None:
                raise RuntimeError("Pi child process-exit cleanup is not configured")
            await terminate_children(self.tracker, "pi_process_exit_with_tracked_children")
            self.tracked_cleanup_reason = "pi_process_exit_with_tracked_children"

    def fallback_error_without_recorded_outcome(self) -> str | None:
        if self.tracker.notification_failure_error is not None:
            return self.tracker.notification_failure_error
        return None

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision:
        if self.pending_children_at_exit():
            await self.cleanup_pending_children_at_exit()
            if recorded_outcome is None:
                return DrainExitDecision(
                    recorded_outcome=_terminal_outcome(
                        status="failed",
                        exit_code=1,
                        error="pi_process_exited_with_tracked_children",
                    )
                )
        return DrainExitDecision(
            recorded_outcome=recorded_outcome,
            fallback_error=self.fallback_error_without_recorded_outcome(),
        )

    def after_finalized(
        self,
        *,
        connection_session_id: str | None,
        outcome: DrainOutcome,
    ) -> None:
        session_id = self.finalization_session_id(connection_session_id)
        self.emit_session_phase_if_needed(session_id)
        self.emit_finalized(
            status=outcome.status,
            exit_code=outcome.exit_code,
            error=outcome.error,
        )

    def finalization_session_id(self, connection_session_id: str | None) -> str | None:
        return connection_session_id if self.session_seen else None

    def emit_session_phase_if_needed(self, session_id: str | None) -> None:
        if self.session_phase_emitted:
            return
        self._emit(
            "session_event_seen" if self.session_seen else "session_event_absent",
            session_id=session_id if self.session_seen else None,
        )

    def emit_finalized(self, *, status: str, exit_code: int, error: str | None) -> None:
        self._emit("finalized", status=status, exit_code=exit_code, error=error)

    def has_pending_children(self) -> bool:
        return self.tracker.has_pending() or self.quiescence_tracker.has_pending_child_spawns()

    def classify_outstanding_work(self) -> PiOutstandingWork:
        from meridian.lib.state import spawn_store
        from meridian.lib.state.spawn.model import SpawnRecord
        from meridian.lib.state.spawn_tree import (
            has_outstanding_descendant_work,
            iter_descendants_from_parent_map,
        )

        try:
            rows = spawn_store.list_spawns(self.runtime_root)
        except Exception:
            # If the core store cannot produce a valid row view, fail closed:
            # do not nudge as though only Pi-internal work remains.
            return PiOutstandingWork(
                spawn_children=True,
                non_spawn_processes=self.quiescence_tracker.has_tracked_bash_bg()
                or self.tracker.has_pending(),
            )
        by_parent: dict[str | None, list[SpawnRecord]] = {}
        for row in rows:
            by_parent.setdefault(row.parent_id, []).append(row)
        active_descendant_ids = {
            child.id
            for child in iter_descendants_from_parent_map(str(self.spawn_id), by_parent)
            if is_active_spawn_status(child.status)
        }
        rowless_tracked_subspawn_ids = self.tracker.active_ids - active_descendant_ids
        rowless_meridian_spawn_ids = {
            subspawn_id
            for subspawn_id in rowless_tracked_subspawn_ids
            if spawn_store.is_spawn_id_shape(subspawn_id)
        }
        rowless_pi_internal_subspawns = bool(
            rowless_tracked_subspawn_ids - rowless_meridian_spawn_ids
        )
        return PiOutstandingWork(
            spawn_children=has_outstanding_descendant_work(str(self.spawn_id), rows),
            unknown_spawn_children=bool(rowless_meridian_spawn_ids)
            or self.quiescence_tracker.has_pending_child_spawns(),
            non_spawn_processes=(
                self.quiescence_tracker.has_tracked_bash_bg() or rowless_pi_internal_subspawns
            ),
        )

    def pending_child_count(self) -> int:
        return (
            self.tracker.active_tracked_count()
            + self.quiescence_tracker.pending_child_spawn_count()
        )

    def is_quiescent(self) -> bool:
        return (
            self.quiescence_tracker.is_quiescent()
            and not self.tracker.has_pending()
            and not self.tracker.has_pending_notifications()
        )

    def emit_waiting_phases_if_needed(self) -> None:
        if not self.quiescence_enabled:
            return
        waiting_for_children = self.has_pending_children()
        child_count = self.pending_child_count()
        if waiting_for_children:
            if child_count != self.waiting_child_count:
                self._emit(
                    "waiting_for_tracked_children",
                    active_tracked_count=child_count,
                )
            self.waiting_child_count = child_count
        else:
            self.waiting_child_count = None

        waiting_for_notifications = (
            not waiting_for_children and self.tracker.has_pending_notifications()
        )
        notification_count = self.tracker.pending_notification_count()
        if waiting_for_notifications:
            if notification_count != self.waiting_notification_count:
                self._emit(
                    "waiting_for_notification_completion",
                    pending_notification_count=notification_count,
                )
            self.waiting_notification_count = notification_count
        else:
            self.waiting_notification_count = None

    def start_micro_drain(self, outcome: TerminalEventOutcome) -> None:
        self.quiescence_candidate = outcome
        self.micro_drain_event_count = 0
        self._clear_done_nudge_timer()
        self._emit("quiescence_micro_drain_started")

    def _consume_done_signal(self) -> DrainLoopDecision | None:
        if self.last_successful_terminal is None:
            return None
        signals = consume_resident_signals(self.runtime_root, self.spawn_id)
        if not signals.done:
            return None
        self._clear_done_nudge_timer()
        return DrainLoopDecision(self.last_successful_terminal)

    def _refresh_done_nudge_state(self) -> None:
        if (
            not self.quiescence_enabled
            or self.last_successful_terminal is None
            or not self.quiescence_tracker.parent_idle
            or self.quiescence_candidate is not None
        ):
            self._clear_done_nudge_timer()
            return
        outstanding = self.classify_outstanding_work()
        if (
            outstanding.spawn_children
            or outstanding.unknown_spawn_children
            or not outstanding.non_spawn_processes
        ):
            self._clear_done_nudge_timer()
            return
        if self.next_done_nudge_monotonic is None:
            delay = max(0.0, self.done_nudge_idle_delay_seconds)
            self.next_done_nudge_monotonic = time.monotonic() + delay

    def _done_nudge_remaining(self, now_monotonic: float) -> float | None:
        self._refresh_done_nudge_state()
        if self.next_done_nudge_monotonic is None:
            return None
        remaining = self.next_done_nudge_monotonic - now_monotonic
        return remaining if remaining > 0 else _PI_TIMEOUT_FLOOR_SECONDS

    def _done_nudge_due(self, now_monotonic: float) -> bool:
        self._refresh_done_nudge_state()
        return (
            self.next_done_nudge_monotonic is not None
            and now_monotonic >= self.next_done_nudge_monotonic
        )

    async def _send_done_nudge(self) -> None:
        try:
            if self.send_done_nudge is not None:
                await self.send_done_nudge(PI_COMPLETION_NUDGE_MESSAGE)
            else:
                await self.receiver.send_user_message(PI_COMPLETION_NUDGE_MESSAGE)
        except Exception:
            # Completion nudges are advisory; drain-loop correctness must not depend on them.
            return

    def _clear_done_nudge_timer(self) -> None:
        self.next_done_nudge_monotonic = None

    def _notification_timeout_outcome(
        self,
        expired_notification: PiPendingNotification,
        now_monotonic: float,
    ) -> TerminalEventOutcome:
        timeout_error = _pi_notification_timeout_error(
            expired_notification,
            now_monotonic=now_monotonic,
        )
        self.tracker.notification_timeout_error = timeout_error
        self._emit(
            "pi_notification_timeout",
            notification_id=expired_notification.notification_id,
            notification_phase=expired_notification.phase,
            timeout_seconds=(
                expired_notification.deadline_monotonic - expired_notification.started_monotonic
                if expired_notification.deadline_monotonic is not None
                else None
            ),
        )
        return _terminal_outcome(status="failed", exit_code=1, error=timeout_error)

    async def _handle_child_wave_timeout(
        self,
        terminate_children: TerminatePiChildren,
        now_monotonic: float,
    ) -> None:
        elapsed_seconds = 0.0
        if self.child_wave_started_monotonic is not None:
            elapsed_seconds = max(0.0, now_monotonic - self.child_wave_started_monotonic)
        timeout_seconds = 0.0
        if (
            self.child_wave_deadline_monotonic is not None
            and self.child_wave_started_monotonic is not None
        ):
            timeout_seconds = max(
                0.0,
                self.child_wave_deadline_monotonic - self.child_wave_started_monotonic,
            )

        cleanup_needed = self.tracked_cleanup_reason is None
        if cleanup_needed:
            self.tracked_cleanup_reason = "pi_child_wave_timeout"
        self._clear_child_wave_timer()
        if cleanup_needed:
            try:
                await terminate_children(self.tracker, "pi_child_wave_timeout")
            except Exception as exc:
                self.tracked_cleanup_error = str(exc)
            finally:
                tracked_count = (
                    self.tracker.clear_tracked_children_after_wave_timeout()
                    + self.quiescence_tracker.pending_child_spawn_count()
                )
                self.waiting_child_count = None
        else:
            tracked_count = (
                self.tracker.clear_tracked_children_after_wave_timeout()
                + self.quiescence_tracker.pending_child_spawn_count()
            )
        timeout_payload: dict[str, object] = {
            "active_tracked_count": tracked_count,
            "elapsed_seconds": elapsed_seconds,
            "timeout_seconds": timeout_seconds,
        }
        if self.tracked_cleanup_error is not None:
            timeout_payload["cleanup_error"] = self.tracked_cleanup_error
        try:
            self._emit("pi_child_wave_timeout", **timeout_payload)
        except Exception:
            logger.warning("Failed to emit Pi child-wave timeout phase", exc_info=True)

    def _child_wave_remaining(self, now_monotonic: float) -> float | None:
        deadline = self.child_wave_deadline_monotonic
        if (
            deadline is None
            or not self.quiescence_tracker.parent_idle
            or not self.has_pending_children()
        ):
            return None
        remaining = deadline - now_monotonic
        return remaining if remaining > 0 else _PI_TIMEOUT_FLOOR_SECONDS

    def _child_wave_timed_out(self, now_monotonic: float) -> bool:
        return (
            self.child_wave_deadline_monotonic is not None
            and self.quiescence_tracker.parent_idle
            and self.has_pending_children()
            and now_monotonic >= self.child_wave_deadline_monotonic
        )

    def _clear_child_wave_timer(self) -> None:
        self.child_wave_deadline_monotonic = None
        self.child_wave_started_monotonic = None

    def _emit(self, phase: str, **payload: object) -> None:
        self.emit_phase(phase=phase, session_role=self.session_role or None, **payload)


def _pi_notification_timeout_error(
    pending: PiPendingNotification,
    *,
    now_monotonic: float,
) -> str:
    timeout_seconds = 0.0
    if pending.deadline_monotonic is not None:
        timeout_seconds = max(0.0, pending.deadline_monotonic - pending.started_monotonic)
    elapsed_seconds = max(0.0, now_monotonic - pending.started_monotonic)
    return (
        "pi_notification_timeout:"
        f"id={pending.notification_id}:"
        f"phase={pending.phase}:"
        f"elapsed={elapsed_seconds:.3f}:"
        f"timeout={timeout_seconds:.3f}"
    )


def _pi_child_wave_timeout_error() -> str:
    return "pi_child_wave_timeout"


def _min_timeout(current: float | None, candidate: float) -> float:
    bounded = candidate if candidate > 0 else _PI_TIMEOUT_FLOOR_SECONDS
    return bounded if current is None else min(current, bounded)


def _terminal_outcome(
    *,
    status: SpawnStatus,
    exit_code: int,
    error: str | None,
) -> TerminalEventOutcome:
    from meridian.lib.harness.semantics import TerminalEventOutcome

    return TerminalEventOutcome(status=status, exit_code=exit_code, error=error)
