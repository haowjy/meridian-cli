"""Pi-private liveness evidence and cleanup ownership.

Persisted Meridian descendants belong to the reconciled spawn tree. This ledger
owns the remaining Pi-specific work that can block completion or require process
cleanup: lifecycle-observed subspawns, managed bash, implicit-wait notifications,
and private-ledger read failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.streaming.completion_contracts import EvidenceFailure


@dataclass(frozen=True, slots=True)
class PiPendingNotification:
    """Immutable notification lifecycle evidence."""

    notification_id: str
    phase: str
    started_monotonic: float
    deadline_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class PiCleanupHandle:
    """Process-group handle owned by one lifecycle-observed subspawn."""

    subspawn_id: str
    process_group_id: int


@dataclass(frozen=True, slots=True)
class PiPrivateWorkBlocker:
    """Categorized Pi-private blocker with its compatibility diagnostic code."""

    kind: Literal[
        "rowless_subspawn",
        "tracked_bash",
        "notification",
        "disk_notification",
    ]
    code: str
    identity: str | None = None


@dataclass(frozen=True, slots=True)
class PiPrivateWorkSnapshot:
    """Point-in-time, immutable view of Pi-private completion blockers."""

    rowless_subspawn_ids: tuple[str, ...]
    tracked_bash_bg: bool
    pending_notifications: tuple[PiPendingNotification, ...]
    pending_disk_notification: bool
    blockers: tuple[PiPrivateWorkBlocker, ...]
    failure: EvidenceFailure | None


class PiPrivateWorkLedger:
    """Sole mutable owner of Pi-private work and cleanup handles."""

    def __init__(self) -> None:
        self._tracked_subspawn_ids: set[str] = set()
        self._persisted_subspawn_ids: set[str] = set()
        self._cleanup_handles: dict[str, PiCleanupHandle] = {}
        self._pending_notifications: dict[str, PiPendingNotification] = {}
        self._tracked_bash_bg = False
        self._last_notification_ts: float | None = None
        self._read_failures: dict[Path, EvidenceFailure] = {}

    def note_subspawn_started(
        self,
        subspawn_id: str,
        *,
        process_group_id: int | None,
    ) -> None:
        self._tracked_subspawn_ids.add(subspawn_id)
        if process_group_id is not None:
            self._cleanup_handles[subspawn_id] = PiCleanupHandle(
                subspawn_id=subspawn_id,
                process_group_id=process_group_id,
            )

    def note_subspawn_ended(self, subspawn_id: str) -> None:
        self._tracked_subspawn_ids.discard(subspawn_id)
        self._persisted_subspawn_ids.discard(subspawn_id)
        self._cleanup_handles.pop(subspawn_id, None)

    def note_persisted_subspawn(self, subspawn_id: str) -> None:
        """Classify a lifecycle ID as tree-owned without dropping its cleanup handle."""
        self._persisted_subspawn_ids.add(subspawn_id)

    def tracked_subspawn_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tracked_subspawn_ids))

    def rowless_subspawn_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tracked_subspawn_ids - self._persisted_subspawn_ids))

    def active_tracked_count(self) -> int:
        return len(self._tracked_subspawn_ids)

    def cleanup_handles(
        self,
        *,
        exclude_ids: set[str] | None = None,
    ) -> tuple[PiCleanupHandle, ...]:
        excluded = exclude_ids or set()
        return tuple(
            sorted(
                (
                    handle
                    for subspawn_id, handle in self._cleanup_handles.items()
                    if subspawn_id in self._tracked_subspawn_ids
                    and subspawn_id not in excluded
                    and handle.process_group_id > 0
                ),
                key=lambda handle: (handle.process_group_id, handle.subspawn_id),
            )
        )

    def active_tracked_pgid_candidates(
        self,
        *,
        exclude_ids: set[str] | None = None,
    ) -> tuple[int, ...]:
        """Preserve the legacy cleanup callback port during ledger cutover."""
        return tuple(
            sorted(
                {
                    handle.process_group_id
                    for handle in self.cleanup_handles(exclude_ids=exclude_ids)
                }
            )
        )

    def clear_tracked_subspawns(self) -> int:
        tracked_count = len(self._tracked_subspawn_ids)
        self._tracked_subspawn_ids.clear()
        self._persisted_subspawn_ids.clear()
        self._cleanup_handles.clear()
        return tracked_count

    def note_notification_started(
        self,
        notification_id: str,
        *,
        phase: str,
        observation_monotonic: float,
        notification_timeout_seconds: float | None,
    ) -> None:
        current = self._pending_notifications.get(notification_id)
        started_monotonic = (
            current.started_monotonic if current is not None else observation_monotonic
        )
        deadline_monotonic = (
            None
            if notification_timeout_seconds is None or notification_timeout_seconds <= 0
            else started_monotonic + notification_timeout_seconds
        )
        self._pending_notifications[notification_id] = PiPendingNotification(
            notification_id=notification_id,
            phase=phase,
            started_monotonic=started_monotonic,
            deadline_monotonic=deadline_monotonic,
        )

    def note_notification_ended(self, notification_id: str) -> None:
        self._pending_notifications.pop(notification_id, None)

    def pending_notifications(self) -> tuple[PiPendingNotification, ...]:
        return tuple(
            self._pending_notifications[notification_id]
            for notification_id in sorted(self._pending_notifications)
        )

    def has_pending_notifications(self) -> bool:
        return bool(self._pending_notifications)

    def pending_notification_count(self) -> int:
        return len(self._pending_notifications)

    def resolve_notification_on_terminal(
        self,
        notification_id: str | None,
    ) -> str | None:
        if not self._pending_notifications:
            return None
        if notification_id is not None and notification_id in self._pending_notifications:
            self._pending_notifications.pop(notification_id, None)
            return notification_id

        delivered = [
            pending.notification_id
            for pending in self._pending_notifications.values()
            if pending.phase == "delivered"
        ]
        if len(delivered) != 1:
            return None
        resolved_id = delivered[0]
        self._pending_notifications.pop(resolved_id, None)
        return resolved_id

    def time_until_next_notification_timeout(self, now_monotonic: float) -> float | None:
        remaining: float | None = None
        for pending in self._pending_notifications.values():
            deadline = pending.deadline_monotonic
            if deadline is None:
                continue
            current_remaining = deadline - now_monotonic
            if remaining is None or current_remaining < remaining:
                remaining = current_remaining
        return remaining

    def pop_expired_notification(self, now_monotonic: float) -> PiPendingNotification | None:
        expired: PiPendingNotification | None = None
        for pending in self._pending_notifications.values():
            deadline = pending.deadline_monotonic
            if deadline is None or deadline > now_monotonic:
                continue
            if expired is None or pending.started_monotonic < expired.started_monotonic:
                expired = pending
        if expired is None:
            return None
        self._pending_notifications.pop(expired.notification_id, None)
        return expired

    def update_disk_evidence(
        self,
        *,
        tracked_bash_bg: bool,
        last_notification_ts: float | None,
    ) -> None:
        self._tracked_bash_bg = tracked_bash_bg
        self._last_notification_ts = last_notification_ts

    def tracked_bash_bg(self) -> bool:
        return self._tracked_bash_bg

    def last_notification_ts(self) -> float | None:
        return self._last_notification_ts

    def record_read_failure(self, path: Path, detail: str) -> bool:
        failure = EvidenceFailure(
            code="pi_private_work_read_failed",
            detail=f"{path.as_posix()}: {detail}",
        )
        if self._read_failures.get(path) == failure:
            return False
        self._read_failures[path] = failure
        return True

    def clear_read_failure(self, path: Path) -> bool:
        return self._read_failures.pop(path, None) is not None

    def evidence_failure(self) -> EvidenceFailure | None:
        if not self._read_failures:
            return None
        return self._read_failures[min(self._read_failures, key=str)]

    def blocker_snapshot(
        self,
        *,
        parent_idle_epoch: float | None,
    ) -> PiPrivateWorkSnapshot:
        last_notification_ts = self._last_notification_ts
        pending_disk_notification = bool(
            parent_idle_epoch is not None
            and last_notification_ts is not None
            and parent_idle_epoch <= last_notification_ts
        )
        rowless_subspawn_ids = self.rowless_subspawn_ids()
        pending_notifications = self.pending_notifications()
        blockers: list[PiPrivateWorkBlocker] = [
            PiPrivateWorkBlocker(
                kind="rowless_subspawn",
                code="pi_tracked_child",
                identity=subspawn_id,
            )
            for subspawn_id in rowless_subspawn_ids
        ]
        if self._tracked_bash_bg:
            blockers.append(PiPrivateWorkBlocker(kind="tracked_bash", code="pi_tracked_bash_bg"))
        blockers.extend(
            PiPrivateWorkBlocker(
                kind="notification",
                code="pi_pending_notification",
                identity=pending.notification_id,
            )
            for pending in pending_notifications
        )
        if pending_disk_notification:
            blockers.append(
                PiPrivateWorkBlocker(
                    kind="disk_notification",
                    code="pi_disk_notification",
                )
            )
        return PiPrivateWorkSnapshot(
            rowless_subspawn_ids=rowless_subspawn_ids,
            tracked_bash_bg=self._tracked_bash_bg,
            pending_notifications=pending_notifications,
            pending_disk_notification=pending_disk_notification,
            blockers=tuple(blockers),
            failure=self.evidence_failure(),
        )
