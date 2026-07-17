"""Disk-backed Pi-private liveness evidence.

Persisted Meridian descendants belong to the reconciled spawn tree. This ledger
owns only the remaining Pi-specific disk evidence: managed bash, the direct
follow-up marker, and private-file read failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.streaming.completion_contracts import EvidenceFailure


@dataclass(frozen=True, slots=True)
class PiPrivateWorkBlocker:
    """Categorized Pi-private blocker with its diagnostic code."""

    kind: Literal["tracked_bash", "disk_notification"]
    code: str


@dataclass(frozen=True, slots=True)
class PiPrivateWorkSnapshot:
    """Point-in-time, immutable view of Pi-private completion blockers."""

    tracked_bash_bg: bool
    pending_disk_notification: bool
    blockers: tuple[PiPrivateWorkBlocker, ...]
    failure: EvidenceFailure | None


class PiPrivateWorkLedger:
    """Sole mutable owner of Pi-private disk evidence."""

    def __init__(self) -> None:
        self._tracked_bash_bg = False
        self._last_notification_ts: float | None = None
        self._read_failures: dict[Path, EvidenceFailure] = {}

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
        pending_disk_notification = bool(
            parent_idle_epoch is not None
            and self._last_notification_ts is not None
            and parent_idle_epoch <= self._last_notification_ts
        )
        blockers: list[PiPrivateWorkBlocker] = []
        if self._tracked_bash_bg:
            blockers.append(PiPrivateWorkBlocker(kind="tracked_bash", code="pi_tracked_bash_bg"))
        if pending_disk_notification:
            blockers.append(
                PiPrivateWorkBlocker(kind="disk_notification", code="pi_disk_notification")
            )
        return PiPrivateWorkSnapshot(
            tracked_bash_bg=self._tracked_bash_bg,
            pending_disk_notification=pending_disk_notification,
            blockers=tuple(blockers),
            failure=self.evidence_failure(),
        )
