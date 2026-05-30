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
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.streaming.drain_policy import DrainAction, DrainPolicy, PiRpcQuiescenceDrainPolicy
from meridian.lib.streaming.pi_quiescence import PiQuiescenceTracker

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)

_PI_CANONICAL_DEDUP_LIFECYCLE_EVENTS = pi_lifecycle.PI_CANONICAL_DEDUP_LIFECYCLE_EVENTS
_PI_CANONICAL_LIFECYCLE_EVENT_PREFIXES = pi_lifecycle.PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES
_PI_CANONICAL_NOTIFICATION_EVENTS = pi_lifecycle.PI_CANONICAL_NOTIFICATION_EVENTS
_PI_CANONICAL_SUBSPAWN_END_EVENTS = pi_lifecycle.PI_CANONICAL_SUBSPAWN_END_EVENTS
_PI_CANONICAL_SUBSPAWN_START_EVENTS = pi_lifecycle.PI_CANONICAL_SUBSPAWN_START_EVENTS
_PI_LEGACY_SUBSPAWN_END_EVENTS = pi_lifecycle.PI_LEGACY_SUBSPAWN_END_EVENTS
_PI_LEGACY_SUBSPAWN_START_EVENTS = pi_lifecycle.PI_LEGACY_SUBSPAWN_START_EVENTS
_PI_NOTIFICATION_COMPLETED_EVENTS = pi_lifecycle.PI_NOTIFICATION_COMPLETED_EVENTS
_PI_NOTIFICATION_DELIVERED_EVENTS = pi_lifecycle.PI_NOTIFICATION_DELIVERED_EVENTS
_PI_NOTIFICATION_FAILED_EVENTS = pi_lifecycle.PI_NOTIFICATION_FAILED_EVENTS
_PI_NOTIFICATION_QUEUED_EVENTS = pi_lifecycle.PI_NOTIFICATION_QUEUED_EVENTS
_PI_PHASE_EVENT_TYPE = pi_lifecycle.PI_PHASE_EVENT_TYPE
_PI_SUBSPAWN_END_EVENTS = pi_lifecycle.PI_SUBSPAWN_END_EVENTS
_PI_SUBSPAWN_START_EVENTS = pi_lifecycle.PI_SUBSPAWN_START_EVENTS
_canonical_lifecycle_label = pi_lifecycle.canonical_pi_lifecycle_label
_event_label_candidates = pi_lifecycle.pi_lifecycle_event_label_candidates
_is_legacy_notification_label = pi_lifecycle.is_legacy_pi_notification_label
_pi_correlation_id = pi_lifecycle.pi_correlation_id
_pi_notification_failure_error = pi_lifecycle.pi_notification_failure_error
_pi_notification_id = pi_lifecycle.pi_notification_id
_pi_subspawn_id = pi_lifecycle.pi_subspawn_id
_pi_subspawn_pgid = pi_lifecycle.pi_subspawn_pgid
_pi_subspawn_pid = pi_lifecycle.pi_subspawn_pid
_pi_wait_policy_is_tracked = pi_lifecycle.pi_wait_policy_is_tracked
_unsupported_pi_schema_version_error = pi_lifecycle.unsupported_pi_schema_version_error

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
class PiPendingNotification:
    notification_id: str
    phase: str
    started_monotonic: float
    deadline_monotonic: float | None = None


@dataclass
class PiSubspawnTracker:
    active_ids: set[str]
    active_process_groups: dict[str, int]
    canonical_event_keys: set[tuple[str, str, str]]
    resolved_subspawn_ids: set[str]
    anonymous_active_count: int = 0
    pending_notifications: dict[str, PiPendingNotification] | None = None
    anonymous_pending_notifications: int = 0
    notification_failure_error: str | None = None
    notification_timeout_error: str | None = None
    lifecycle_tracking_invalidated_error: str | None = None

    @classmethod
    def empty(cls) -> PiSubspawnTracker:
        return cls(
            active_ids=set(),
            active_process_groups={},
            canonical_event_keys=set(),
            resolved_subspawn_ids=set(),
            pending_notifications={},
        )

    def observe(
        self,
        event: HarnessEvent,
        *,
        now_monotonic: float | None = None,
        notification_timeout_seconds: float | None = None,
    ) -> bool:
        """Update tracker state from one Pi event.

        Returns True when the event is a duplicate canonical lifecycle event and
        should be treated as diagnostic-only by callers.
        """
        if event.harness_id != HarnessId.PI.value:
            return False
        observation_monotonic = now_monotonic if now_monotonic is not None else time.monotonic()

        labels = _event_label_candidates(event)
        label_set = set(labels)
        lifecycle_schema_error = _unsupported_pi_schema_version_error(label_set, event.payload)
        if lifecycle_schema_error is not None:
            self.lifecycle_tracking_invalidated_error = lifecycle_schema_error
            return False
        if self._is_parse_error_for_canonical_lifecycle_event(event):
            raw_type = event.payload.get("raw_type")
            raw_type_text = raw_type if isinstance(raw_type, str) else "unknown"
            self.lifecycle_tracking_invalidated_error = (
                "pi_lifecycle_tracking_invalidated:"
                f"unsupported_schema_event:{raw_type_text}"
            )
            return False
        dedup_key = self._canonical_lifecycle_dedup_key(label_set, event.payload)
        if dedup_key is not None:
            if dedup_key in self.canonical_event_keys:
                logger.debug(
                    "Ignoring duplicate canonical Pi lifecycle event",
                    extra={
                        "event_type": dedup_key[0],
                        "correlation_id": dedup_key[1],
                        "event_specific_id": dedup_key[2],
                    },
                )
                return True
            self.canonical_event_keys.add(dedup_key)

        is_subspawn_start = bool(label_set & _PI_SUBSPAWN_START_EVENTS)
        if is_subspawn_start:
            if not _pi_wait_policy_is_tracked(event.payload):
                return False
            has_canonical_label = bool(label_set & _PI_CANONICAL_SUBSPAWN_START_EVENTS)
            has_legacy_label = bool(label_set & _PI_LEGACY_SUBSPAWN_START_EVENTS)
            subspawn_id = _pi_subspawn_id(event.payload)
            if subspawn_id is not None:
                if subspawn_id in self.resolved_subspawn_ids:
                    logger.debug(
                        "Ignoring duplicate subspawn.start after terminal subspawn.end",
                        extra={"subspawn_id": subspawn_id},
                    )
                    return False
                self.active_ids.add(subspawn_id)
                pgid = _pi_subspawn_pgid(event.payload)
                pid = _pi_subspawn_pid(event.payload)
                process_group_id = pgid if pgid is not None else pid
                if process_group_id is not None:
                    self.active_process_groups[subspawn_id] = process_group_id
            elif has_canonical_label:
                canonical_label = _canonical_lifecycle_label(
                    label_set,
                    _PI_CANONICAL_SUBSPAWN_START_EVENTS,
                )
                self.lifecycle_tracking_invalidated_error = (
                    "pi_lifecycle_tracking_invalidated:"
                    f"missing_subspawn_id:{canonical_label}"
                )
            elif has_legacy_label and not has_canonical_label:
                self.anonymous_active_count += 1
            return False

        is_subspawn_end = bool(label_set & _PI_SUBSPAWN_END_EVENTS)
        if is_subspawn_end:
            if not _pi_wait_policy_is_tracked(event.payload):
                return False
            has_canonical_label = bool(label_set & _PI_CANONICAL_SUBSPAWN_END_EVENTS)
            has_legacy_label = bool(label_set & _PI_LEGACY_SUBSPAWN_END_EVENTS)
            subspawn_id = _pi_subspawn_id(event.payload)
            if subspawn_id is not None:
                if subspawn_id in self.resolved_subspawn_ids:
                    logger.debug(
                        "Ignoring duplicate subspawn.end after terminal subspawn.end",
                        extra={"subspawn_id": subspawn_id},
                    )
                    return False
                self.resolved_subspawn_ids.add(subspawn_id)
                self.active_ids.discard(subspawn_id)
                self.active_process_groups.pop(subspawn_id, None)
                return False
            if has_canonical_label:
                canonical_label = _canonical_lifecycle_label(
                    label_set,
                    _PI_CANONICAL_SUBSPAWN_END_EVENTS,
                )
                self.lifecycle_tracking_invalidated_error = (
                    "pi_lifecycle_tracking_invalidated:"
                    f"missing_subspawn_id:{canonical_label}"
                )
                return False
            if has_legacy_label and not has_canonical_label and self.anonymous_active_count > 0:
                self.anonymous_active_count -= 1
            return False

        pending_notifications = self.pending_notifications
        if pending_notifications is None:
            return False

        is_notification_start = bool(
            label_set & (_PI_NOTIFICATION_QUEUED_EVENTS | _PI_NOTIFICATION_DELIVERED_EVENTS)
        )
        if is_notification_start:
            notification_id = _pi_notification_id(event.payload)
            if notification_id is not None:
                phase = (
                    "delivered"
                    if bool(label_set & _PI_NOTIFICATION_DELIVERED_EVENTS)
                    else "queued"
                )
                current = pending_notifications.get(notification_id)
                started_monotonic = (
                    current.started_monotonic if current is not None else observation_monotonic
                )
                deadline_monotonic = (
                    None
                    if notification_timeout_seconds is None or notification_timeout_seconds <= 0
                    else started_monotonic + notification_timeout_seconds
                )
                pending_notifications[notification_id] = PiPendingNotification(
                    notification_id=notification_id,
                    phase=phase,
                    started_monotonic=started_monotonic,
                    deadline_monotonic=deadline_monotonic,
                )
            elif label_set & _PI_CANONICAL_NOTIFICATION_EVENTS:
                canonical_label = _canonical_lifecycle_label(
                    label_set,
                    _PI_CANONICAL_NOTIFICATION_EVENTS,
                )
                self.lifecycle_tracking_invalidated_error = (
                    "pi_lifecycle_tracking_invalidated:"
                    f"missing_notification_id:{canonical_label}"
                )
            elif _is_legacy_notification_label(label_set):
                self.anonymous_pending_notifications += 1
            return False

        is_notification_end = bool(
            label_set & (_PI_NOTIFICATION_COMPLETED_EVENTS | _PI_NOTIFICATION_FAILED_EVENTS)
        )
        if is_notification_end:
            notification_id = _pi_notification_id(event.payload)
            if notification_id is not None:
                pending_notifications.pop(notification_id, None)
            elif label_set & _PI_CANONICAL_NOTIFICATION_EVENTS:
                canonical_label = _canonical_lifecycle_label(
                    label_set,
                    _PI_CANONICAL_NOTIFICATION_EVENTS,
                )
                self.lifecycle_tracking_invalidated_error = (
                    "pi_lifecycle_tracking_invalidated:"
                    f"missing_notification_id:{canonical_label}"
                )
                return False
            elif (
                _is_legacy_notification_label(label_set)
                and self.anonymous_pending_notifications > 0
            ):
                self.anonymous_pending_notifications -= 1
            if label_set & _PI_NOTIFICATION_FAILED_EVENTS:
                self.notification_failure_error = _pi_notification_failure_error(event.payload)
        return False

    def _canonical_lifecycle_dedup_key(
        self,
        labels: set[str],
        payload: dict[str, object],
    ) -> tuple[str, str, str] | None:
        canonical_labels = sorted(
            label for label in labels if label in _PI_CANONICAL_DEDUP_LIFECYCLE_EVENTS
        )
        if not canonical_labels:
            return None
        event_type = canonical_labels[0]
        correlation_id = _pi_correlation_id(payload)
        if correlation_id is None:
            return None
        event_specific_id = ""
        if event_type.startswith("meridian.subspawn."):
            subspawn_id = _pi_subspawn_id(payload)
            if subspawn_id is not None:
                event_specific_id = subspawn_id
        elif event_type.startswith("meridian.notification."):
            notification_id = _pi_notification_id(payload)
            if notification_id is not None:
                event_specific_id = notification_id
        return (event_type, correlation_id, event_specific_id)

    def _is_parse_error_for_canonical_lifecycle_event(self, event: HarnessEvent) -> bool:
        labels = _event_label_candidates(event)
        label_set = set(labels)
        if "meridian.lifecycle.parse_error" not in label_set:
            return False
        raw_type = event.payload.get("raw_type")
        if not isinstance(raw_type, str):
            return False
        if not raw_type.startswith(_PI_CANONICAL_LIFECYCLE_EVENT_PREFIXES):
            return False
        parse_error = event.payload.get("error")
        return isinstance(parse_error, str) and parse_error == "unsupported_schema_version"

    def has_pending(self) -> bool:
        return bool(self.active_ids) or self.anonymous_active_count > 0

    def active_tracked_count(self) -> int:
        return len(self.active_ids) + self.anonymous_active_count

    def active_tracked_pgid_candidates(self) -> tuple[int, ...]:
        unique = {
            pgid
            for subspawn_id, pgid in self.active_process_groups.items()
            if subspawn_id in self.active_ids and pgid > 0
        }
        return tuple(sorted(unique))

    def clear_tracked_children_after_wave_timeout(self) -> int:
        tracked_count = self.active_tracked_count()
        self.active_ids.clear()
        self.active_process_groups.clear()
        self.anonymous_active_count = 0
        return tracked_count

    def has_pending_notifications(self) -> bool:
        pending_notifications = self.pending_notifications
        return bool(pending_notifications) or self.anonymous_pending_notifications > 0

    def pending_notification_count(self) -> int:
        pending_notifications = self.pending_notifications
        return len(pending_notifications or ()) + self.anonymous_pending_notifications

    def resolve_notification_on_terminal(self, event: HarnessEvent) -> str | None:
        pending_notifications = self.pending_notifications
        if pending_notifications is None or not pending_notifications:
            return None

        notification_id = _pi_notification_id(event.payload)
        if notification_id is not None and notification_id in pending_notifications:
            pending_notifications.pop(notification_id, None)
            return notification_id

        delivered = [
            pending.notification_id
            for pending in pending_notifications.values()
            if pending.phase == "delivered"
        ]
        if len(delivered) == 1:
            resolved_id = delivered[0]
            pending_notifications.pop(resolved_id, None)
            return resolved_id
        return None

    def time_until_next_notification_timeout(self, now_monotonic: float) -> float | None:
        pending_notifications = self.pending_notifications
        if pending_notifications is None:
            return None
        remaining: float | None = None
        for pending in pending_notifications.values():
            deadline = pending.deadline_monotonic
            if deadline is None:
                continue
            current_remaining = deadline - now_monotonic
            if remaining is None or current_remaining < remaining:
                remaining = current_remaining
        return remaining

    def pop_expired_notification(self, now_monotonic: float) -> PiPendingNotification | None:
        pending_notifications = self.pending_notifications
        if pending_notifications is None:
            return None
        expired: PiPendingNotification | None = None
        for pending in pending_notifications.values():
            deadline = pending.deadline_monotonic
            if deadline is None or deadline > now_monotonic:
                continue
            if expired is None or pending.started_monotonic < expired.started_monotonic:
                expired = pending
        if expired is None:
            return None
        pending_notifications.pop(expired.notification_id, None)
        return expired


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
            if (
                self.child_wave_timeout_seconds is not None
                and self.child_wave_timeout_seconds > 0
                and self.has_pending_children()
            ):
                wave_start = time.monotonic()
                self.child_wave_started_monotonic = wave_start
                self.child_wave_deadline_monotonic = wave_start + self.child_wave_timeout_seconds

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
        self.maybe_start_quiescence_after_event()

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
        lifecycle_count = self.tracker.active_tracked_count()
        return lifecycle_count or self.quiescence_tracker.pending_child_spawn_count()

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
            or self.quiescence_tracker.pending_child_spawn_count()
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
