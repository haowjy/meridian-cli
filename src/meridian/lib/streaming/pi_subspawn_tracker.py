"""Pi lifecycle child and notification tracking."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
from meridian.lib.harness.connections.base import HarnessEvent

logger = logging.getLogger(__name__)

_PI_CANONICAL_DEDUP_LIFECYCLE_EVENTS = pi_lifecycle.PI_CANONICAL_DEDUP_LIFECYCLE_EVENTS
_PI_CANONICAL_LIFECYCLE_EVENT_PREFIXES = pi_lifecycle.PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES
_PI_CANONICAL_NOTIFICATION_EVENTS = pi_lifecycle.PI_CANONICAL_NOTIFICATION_EVENTS
_PI_CANONICAL_SUBSPAWN_END_EVENTS = pi_lifecycle.PI_CANONICAL_SUBSPAWN_END_EVENTS
_PI_CANONICAL_SUBSPAWN_START_EVENTS = pi_lifecycle.PI_CANONICAL_SUBSPAWN_START_EVENTS
_PI_NOTIFICATION_COMPLETED_EVENTS = pi_lifecycle.PI_NOTIFICATION_COMPLETED_EVENTS
_PI_NOTIFICATION_DELIVERED_EVENTS = pi_lifecycle.PI_NOTIFICATION_DELIVERED_EVENTS
_PI_NOTIFICATION_FAILED_EVENTS = pi_lifecycle.PI_NOTIFICATION_FAILED_EVENTS
_PI_NOTIFICATION_QUEUED_EVENTS = pi_lifecycle.PI_NOTIFICATION_QUEUED_EVENTS
_PI_SUBSPAWN_END_EVENTS = pi_lifecycle.PI_SUBSPAWN_END_EVENTS
_PI_SUBSPAWN_START_EVENTS = pi_lifecycle.PI_SUBSPAWN_START_EVENTS
_PI_LIFECYCLE_EVENT_ALLOWLIST = pi_lifecycle.PI_LIFECYCLE_EVENT_ALLOWLIST
_canonical_lifecycle_label = pi_lifecycle.canonical_pi_lifecycle_label
_event_label_candidates = pi_lifecycle.pi_lifecycle_event_label_candidates
_pi_correlation_id = pi_lifecycle.pi_correlation_id
_pi_notification_failure_error = pi_lifecycle.pi_notification_failure_error
_pi_notification_id = pi_lifecycle.pi_notification_id
_pi_subspawn_id = pi_lifecycle.pi_subspawn_id
_pi_subspawn_pgid = pi_lifecycle.pi_subspawn_pgid
_pi_subspawn_pid = pi_lifecycle.pi_subspawn_pid
_pi_wait_policy_is_tracked = pi_lifecycle.pi_wait_policy_is_tracked
_unsupported_pi_schema_version_error = pi_lifecycle.unsupported_pi_schema_version_error


@dataclass
class PiPendingNotification:
    notification_id: str
    phase: str
    started_monotonic: float
    deadline_monotonic: float | None = None


def _is_pi_lifecycle_namespace_label(label: str) -> bool:
    """Return True for Pi lifecycle labels that must not be silently ignored."""
    normalized = label.strip().lower()
    return (
        normalized.startswith("meridian.subspawn.")
        or normalized.startswith("meridian.notification.")
        or normalized.startswith("meridian.quiescence.")
        or normalized.startswith("meridian_subspawn_")
        or normalized.startswith("meridian_notification_")
        or normalized.startswith("meridian_quiescence_")
    )


@dataclass
class PiSubspawnTracker:
    active_ids: set[str]
    active_process_groups: dict[str, int]
    canonical_event_keys: set[tuple[str, str, str]]
    resolved_subspawn_ids: set[str]
    pending_notifications: dict[str, PiPendingNotification] | None = None
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
                f"pi_lifecycle_tracking_invalidated:unsupported_schema_event:{raw_type_text}"
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
                    f"pi_lifecycle_tracking_invalidated:missing_subspawn_id:{canonical_label}"
                )
            return False

        is_subspawn_end = bool(label_set & _PI_SUBSPAWN_END_EVENTS)
        if is_subspawn_end:
            if not _pi_wait_policy_is_tracked(event.payload):
                return False
            has_canonical_label = bool(label_set & _PI_CANONICAL_SUBSPAWN_END_EVENTS)
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
                    f"pi_lifecycle_tracking_invalidated:missing_subspawn_id:{canonical_label}"
                )
                return False
            return False

        pending_notifications = self.pending_notifications
        if pending_notifications is None:
            unknown_lifecycle_label = self._unknown_pi_lifecycle_namespace_label(label_set)
            if unknown_lifecycle_label is not None:
                self.lifecycle_tracking_invalidated_error = (
                    "pi_lifecycle_tracking_invalidated:unsupported_lifecycle_event:"
                    f"{unknown_lifecycle_label}"
                )
            return False

        is_notification_start = bool(
            label_set & (_PI_NOTIFICATION_QUEUED_EVENTS | _PI_NOTIFICATION_DELIVERED_EVENTS)
        )
        if is_notification_start:
            notification_id = _pi_notification_id(event.payload)
            if notification_id is not None:
                phase = (
                    "delivered" if bool(label_set & _PI_NOTIFICATION_DELIVERED_EVENTS) else "queued"
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
                    f"pi_lifecycle_tracking_invalidated:missing_notification_id:{canonical_label}"
                )
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
                    f"pi_lifecycle_tracking_invalidated:missing_notification_id:{canonical_label}"
                )
                return False
            if label_set & _PI_NOTIFICATION_FAILED_EVENTS:
                self.notification_failure_error = _pi_notification_failure_error(event.payload)
            return False

        unknown_lifecycle_label = self._unknown_pi_lifecycle_namespace_label(label_set)
        if unknown_lifecycle_label is not None:
            self.lifecycle_tracking_invalidated_error = (
                "pi_lifecycle_tracking_invalidated:unsupported_lifecycle_event:"
                f"{unknown_lifecycle_label}"
            )
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

    def _unknown_pi_lifecycle_namespace_label(self, labels: set[str]) -> str | None:
        for label in sorted(labels):
            if label in _PI_LIFECYCLE_EVENT_ALLOWLIST:
                continue
            if _is_pi_lifecycle_namespace_label(label):
                return label
        return None

    def has_pending(self) -> bool:
        return bool(self.active_ids)

    def active_tracked_count(self) -> int:
        return len(self.active_ids)

    def active_tracked_pgid_candidates(
        self,
        *,
        exclude_ids: set[str] | None = None,
    ) -> tuple[int, ...]:
        excluded = exclude_ids or set()
        unique = {
            pgid
            for subspawn_id, pgid in self.active_process_groups.items()
            if subspawn_id in self.active_ids and subspawn_id not in excluded and pgid > 0
        }
        return tuple(sorted(unique))

    def clear_tracked_children_after_wave_timeout(self) -> int:
        tracked_count = self.active_tracked_count()
        self.active_ids.clear()
        self.active_process_groups.clear()
        return tracked_count

    def has_pending_notifications(self) -> bool:
        pending_notifications = self.pending_notifications
        return bool(pending_notifications)

    def pending_notification_count(self) -> int:
        pending_notifications = self.pending_notifications
        return len(pending_notifications or ())

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
