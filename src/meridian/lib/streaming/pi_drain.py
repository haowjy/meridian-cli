"""Pi-specific completion coordinator for streaming drains.

The generic spawn manager owns connection/event persistence. This module owns the
Pi policy layered on top: child/background work tracking, notification waits,
child-wave timeout, and quiescence micro-drain.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.streaming.drain_policy import DrainAction, DrainPolicy, PiRpcQuiescenceDrainPolicy
from meridian.lib.streaming.pi_quiescence import PiQuiescenceTracker
from meridian.lib.streaming.pi_subspawn_tracker import PiPendingNotification, PiSubspawnTracker

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.launch.launch_types import ResolvedLaunchSpec

_PI_NOTIFICATION_COMPLETED_EVENTS = pi_lifecycle.PI_NOTIFICATION_COMPLETED_EVENTS
_PI_NOTIFICATION_DELIVERED_EVENTS = pi_lifecycle.PI_NOTIFICATION_DELIVERED_EVENTS
_PI_NOTIFICATION_FAILED_EVENTS = pi_lifecycle.PI_NOTIFICATION_FAILED_EVENTS
_PI_NOTIFICATION_QUEUED_EVENTS = pi_lifecycle.PI_NOTIFICATION_QUEUED_EVENTS
_PI_PHASE_EVENT_TYPE = pi_lifecycle.PI_PHASE_EVENT_TYPE
_event_label_candidates = pi_lifecycle.pi_lifecycle_event_label_candidates

PI_MICRO_DRAIN_TIMEOUT_SECONDS: float = 0.05
# Non-zero floor to prevent tight loops on expired deadlines.
_PI_TIMEOUT_FLOOR_SECONDS: float = 0.001

EmitPiPhase = Callable[..., None]
TerminatePiChildren = Callable[["PiSubspawnTracker", str], Awaitable[None]]


@dataclass(frozen=True)
class PiTerminalDecision:
    recorded_outcome: TerminalEventOutcome | None = None
    emit_turn_boundary: bool = False


@dataclass
class PiDrainCoordinator:
    runtime_root: Path
    spawn_id: SpawnId
    receiver: HarnessConnection[ResolvedLaunchSpec]
    session_role: str
    notification_timeout_seconds: float | None
    child_wave_timeout_seconds: float | None
    emit_phase: EmitPiPhase
    tracker: PiSubspawnTracker
    quiescence_tracker: PiQuiescenceTracker
    is_pi_connection: bool
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
    child_wave_timed_out: bool = False
    child_wave_timeout_error: str | None = None
    child_wave_timeout_followup_deadline: float | None = None
    tracked_cleanup_reason: str | None = None

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
    ) -> PiDrainCoordinator:
        normalized_role = (session_role or "").strip().lower()
        is_pi_connection = receiver.harness_id is HarnessId.PI
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
                is_pi_connection=is_pi_connection,
                session_role=normalized_role,
            ),
            is_pi_connection=is_pi_connection,
        )

    async def start(self) -> None:
        await self.quiescence_tracker.start()
        if self.is_pi_connection:
            self._emit("drain_started")

    async def stop(self) -> None:
        await self.quiescence_tracker.stop()

    def default_policy(self) -> DrainPolicy:
        return PiRpcQuiescenceDrainPolicy(quiescence_check=self.quiescence_tracker.is_quiescent)

    def set_policy(self, policy: DrainPolicy) -> None:
        self.quiescence_enabled = self.is_pi_connection and isinstance(
            policy,
            PiRpcQuiescenceDrainPolicy,
        )

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

        followup_remaining = self._followup_remaining(now_monotonic)
        if followup_remaining is not None:
            next_timeout = _min_timeout(next_timeout, followup_remaining)

        return next_timeout

    async def handle_timeout(
        self,
        terminate_children: TerminatePiChildren,
    ) -> TerminalEventOutcome | None:
        if not self.quiescence_enabled:
            raise TimeoutError
        if self.quiescence_candidate is not None:
            await self.quiescence_tracker.refresh_disk_state()
            if self.is_quiescent():
                return self.quiescence_candidate
            self._emit(
                "quiescence_micro_drain_cancelled",
                reason="disk_state_changed",
            )
            self.quiescence_candidate = None
            self._update_idle_waiting_state()
            return None

        now_monotonic = time.monotonic()
        expired_notification = self.tracker.pop_expired_notification(now_monotonic)
        if expired_notification is not None:
            return self._notification_timeout_outcome(expired_notification, now_monotonic)

        if self._child_wave_timed_out(now_monotonic):
            await self._handle_child_wave_timeout(terminate_children, now_monotonic)
            return None

        if (
            self.child_wave_timeout_followup_deadline is not None
            and now_monotonic >= self.child_wave_timeout_followup_deadline
            and self.child_wave_timed_out
            and self.child_wave_timeout_error is not None
        ):
            return _terminal_outcome(
                status="failed",
                exit_code=1,
                error=self.child_wave_timeout_error,
            )
        return None

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        duplicate = self.tracker.observe(
            event,
            now_monotonic=time.monotonic(),
            notification_timeout_seconds=self.notification_timeout_seconds,
        )
        if duplicate:
            return True
        if not self.is_pi_connection:
            return False

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

        if (
            self.child_wave_timed_out
            and self.child_wave_timeout_error is not None
            and self._is_followup_signal(event)
        ):
            self.child_wave_timed_out = False
            self.child_wave_timeout_error = None
            self.child_wave_timeout_followup_deadline = None
        if not self.has_pending_children():
            self._clear_child_wave_timer()
        self.emit_waiting_phases_if_needed()
        return False

    def note_event_persisted(self, event: HarnessEvent) -> None:
        if self.quiescence_enabled and self.quiescence_candidate is not None:
            self.micro_drain_event_count += 1
            self.quiescence_candidate = None
            self._emit(
                "quiescence_micro_drain_extended",
                event_type=event.event_type,
                micro_drain_events=self.micro_drain_event_count,
            )

    def lifecycle_error_outcome(self) -> TerminalEventOutcome | None:
        if not self.is_pi_connection or self.tracker.lifecycle_tracking_invalidated_error is None:
            return None
        return _terminal_outcome(
            status="failed",
            exit_code=1,
            error=self.tracker.lifecycle_tracking_invalidated_error,
        )

    def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> PiTerminalDecision:
        if not self.is_pi_connection:
            return PiTerminalDecision(
                recorded_outcome=outcome if action.terminate else None,
                emit_turn_boundary=action.emit_turn_boundary,
            )
        if outcome.status == "succeeded":
            self.last_successful_terminal = outcome
            completed_notification_id = self.tracker.resolve_notification_on_terminal(event)
            if completed_notification_id is not None:
                self._emit("continuation_completed", notification_id=completed_notification_id)
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
                return PiTerminalDecision()
            return PiTerminalDecision(recorded_outcome=outcome)
        return PiTerminalDecision(emit_turn_boundary=action.emit_turn_boundary)

    def failure_outcome_after_event(self) -> TerminalEventOutcome | None:
        if not self.is_pi_connection:
            return None
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
        if (
            self.child_wave_timed_out
            and self.child_wave_timeout_error is not None
            and self.child_wave_timeout_followup_deadline is not None
            and time.monotonic() >= self.child_wave_timeout_followup_deadline
        ):
            return _terminal_outcome(
                status="failed",
                exit_code=1,
                error=self.child_wave_timeout_error,
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
        self.emit_waiting_phases_if_needed()

    def pending_children_at_exit(self) -> bool:
        return self.quiescence_enabled and self.has_pending_children()

    async def cleanup_pending_children_at_exit(
        self,
        terminate_children: TerminatePiChildren,
    ) -> None:
        if (
            self.is_pi_connection
            and self.tracker.has_pending()
            and self.tracked_cleanup_reason is None
        ):
            await terminate_children(self.tracker, "pi_process_exit_with_tracked_children")
            self.tracked_cleanup_reason = "pi_process_exit_with_tracked_children"

    def fallback_error_without_recorded_outcome(self) -> str | None:
        if not self.is_pi_connection:
            return None
        if self.tracker.notification_failure_error is not None:
            return self.tracker.notification_failure_error
        if self.child_wave_timeout_error is not None:
            return self.child_wave_timeout_error
        return None

    def emit_session_phase_if_needed(self, session_id: str | None) -> None:
        if not self.is_pi_connection or self.session_phase_emitted:
            return
        self._emit(
            "session_event_seen" if self.session_seen else "session_event_absent",
            session_id=session_id if self.session_seen else None,
        )

    def emit_finalized(self, *, status: str, exit_code: int, error: str | None) -> None:
        if self.is_pi_connection:
            self._emit("finalized", status=status, exit_code=exit_code, error=error)

    async def wait_for_disk_change(self) -> None:
        await self.quiescence_tracker.wait_for_disk_change()

    def has_pending_children(self) -> bool:
        return self.tracker.has_pending() or self.quiescence_tracker.has_pending_child_spawns()

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
                    anonymous_tracked_count=self.tracker.anonymous_active_count,
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
                    anonymous_pending_notification_count=self.tracker.anonymous_pending_notifications,
                )
            self.waiting_notification_count = notification_count
        else:
            self.waiting_notification_count = None

    def start_micro_drain(self, outcome: TerminalEventOutcome) -> None:
        self.quiescence_candidate = outcome
        self.micro_drain_event_count = 0
        self._emit("quiescence_micro_drain_started")

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
        if self.tracked_cleanup_reason is None:
            await terminate_children(self.tracker, "pi_child_wave_timeout")
        tracked_count = (
            self.tracker.clear_tracked_children_after_wave_timeout()
            + self.quiescence_tracker.pending_child_spawn_count()
        )
        self.waiting_child_count = None
        self.tracked_cleanup_reason = "pi_child_wave_timeout"
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
        self._emit(
            "pi_child_wave_timeout",
            active_tracked_count=tracked_count,
            elapsed_seconds=elapsed_seconds,
            timeout_seconds=timeout_seconds,
        )
        self._clear_child_wave_timer()
        self.child_wave_timed_out = True
        self.child_wave_timeout_error = _pi_child_wave_timeout_error()
        followup_timeout_seconds = (
            float(self.child_wave_timeout_seconds)
            if self.child_wave_timeout_seconds is not None and self.child_wave_timeout_seconds > 0
            else 300.0
        )
        self.child_wave_timeout_followup_deadline = now_monotonic + followup_timeout_seconds
        self.emit_waiting_phases_if_needed()

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

    def _followup_remaining(self, now_monotonic: float) -> float | None:
        deadline = self.child_wave_timeout_followup_deadline
        if deadline is None:
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

    def _is_followup_signal(self, event: HarnessEvent) -> bool:
        label_set = set(_event_label_candidates(event))
        return bool(
            label_set
            & (
                _PI_NOTIFICATION_QUEUED_EVENTS
                | _PI_NOTIFICATION_DELIVERED_EVENTS
                | _PI_NOTIFICATION_COMPLETED_EVENTS
                | _PI_NOTIFICATION_FAILED_EVENTS
            )
        )

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
