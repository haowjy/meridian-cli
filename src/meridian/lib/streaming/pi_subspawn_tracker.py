"""Pi lifecycle child and notification tracking."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.streaming.pi_work_ledger import (
    PiCleanupHandle,
    PiPendingNotification,
    PiPrivateWorkLedger,
)

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
    _ledger: PiPrivateWorkLedger
    canonical_event_keys: set[tuple[str, str, str]]
    resolved_subspawn_ids: set[str]
    notification_failure_error: str | None = None
    lifecycle_tracking_invalidated_error: str | None = None

    @classmethod
    def empty(cls, ledger: PiPrivateWorkLedger | None = None) -> PiSubspawnTracker:
        return cls(
            _ledger=ledger or PiPrivateWorkLedger(),
            canonical_event_keys=set(),
            resolved_subspawn_ids=set(),
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
                pgid = _pi_subspawn_pgid(event.payload)
                pid = _pi_subspawn_pid(event.payload)
                process_group_id = pgid if pgid is not None else pid
                self._ledger.note_subspawn_started(
                    subspawn_id,
                    process_group_id=process_group_id,
                )
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
                self._ledger.note_subspawn_ended(subspawn_id)
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

        is_notification_start = bool(
            label_set & (_PI_NOTIFICATION_QUEUED_EVENTS | _PI_NOTIFICATION_DELIVERED_EVENTS)
        )
        if is_notification_start:
            notification_id = _pi_notification_id(event.payload)
            if notification_id is not None:
                phase = (
                    "delivered" if bool(label_set & _PI_NOTIFICATION_DELIVERED_EVENTS) else "queued"
                )
                self._ledger.note_notification_started(
                    notification_id,
                    phase=phase,
                    observation_monotonic=observation_monotonic,
                    notification_timeout_seconds=notification_timeout_seconds,
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
                self._ledger.note_notification_ended(notification_id)
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

    # Transitional query wrappers preserve the pre-ledger tracker interface for
    # rollback callers. Production evidence and cleanup consume ledger snapshots
    # and handles directly.
    def has_pending(self) -> bool:
        return self._ledger.active_tracked_count() > 0

    def active_tracked_count(self) -> int:
        return self._ledger.active_tracked_count()

    def active_tracked_ids(self) -> tuple[str, ...]:
        return self._ledger.tracked_subspawn_ids()

    def active_tracked_pgid_candidates(
        self,
        *,
        exclude_ids: set[str] | None = None,
    ) -> tuple[int, ...]:
        unique = {
            handle.process_group_id
            for handle in self._ledger.cleanup_handles(exclude_ids=exclude_ids)
        }
        return tuple(sorted(unique))

    def cleanup_handle_snapshot(
        self,
        *,
        exclude_ids: set[str] | None = None,
    ) -> tuple[PiCleanupHandle, ...]:
        return self._ledger.cleanup_handles(exclude_ids=exclude_ids)

    def clear_tracked_children_after_wave_timeout(self) -> int:
        return self._ledger.clear_tracked_subspawns()

    def has_pending_notifications(self) -> bool:
        return self._ledger.has_pending_notifications()

    def pending_notification_count(self) -> int:
        return self._ledger.pending_notification_count()

    def notification_snapshot(self) -> tuple[PiPendingNotification, ...]:
        return self._ledger.pending_notifications()

    def resolve_notification_on_terminal(self, event: HarnessEvent) -> str | None:
        return self._ledger.resolve_notification_on_terminal(_pi_notification_id(event.payload))

    def time_until_next_notification_timeout(self, now_monotonic: float) -> float | None:
        return self._ledger.time_until_next_notification_timeout(now_monotonic)

    def pop_expired_notification(self, now_monotonic: float) -> PiPendingNotification | None:
        return self._ledger.pop_expired_notification(now_monotonic)
