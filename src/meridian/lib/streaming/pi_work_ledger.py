"""One categorized snapshot of Pi private work and causal delivery obligations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.harness.pi_private_state import BashEvidence
from meridian.lib.streaming.completion_contracts import EvidenceFailure


@dataclass(frozen=True, slots=True)
class PiPrivateWorkBlocker:
    kind: Literal["tracked_bash", "disk_notification"]
    code: str
    identity: str | None = None


@dataclass(frozen=True, slots=True)
class PiPrivateWorkSnapshot:
    tracked_bash_bg: bool
    pending_disk_notification: bool
    blockers: tuple[PiPrivateWorkBlocker, ...]
    failure: EvidenceFailure | None
    consumed_work_ids: frozenset[str] = frozenset()


class PiPrivateWorkLedger:
    """Disk facts are immutable values; liveness and delivery use the same view."""

    def __init__(self) -> None:
        self._bash: tuple[BashEvidence, ...] = ()
        self._consumed: frozenset[str] = frozenset()
        self._unobserved_messages: frozenset[str] = frozenset()
        self._read_failures: dict[Path, EvidenceFailure] = {}

    def update_disk_evidence(
        self,
        *,
        bash: tuple[BashEvidence, ...],
        consumed: frozenset[str],
        unobserved_messages: frozenset[str],
    ) -> None:
        self._bash = bash
        self._consumed = consumed
        self._unobserved_messages = unobserved_messages

    def tracked_bash_bg(self) -> bool:
        return any(r.is_tracked and r.is_background and r.status == "running" for r in self._bash)

    def record_read_failure(self, path: Path, detail: str) -> bool:
        failure = EvidenceFailure(code="pi_private_work_read_failed", detail=f"{path}: {detail}")
        changed = self._read_failures.get(path) != failure
        self._read_failures[path] = failure
        return changed

    def clear_read_failure(self, path: Path) -> bool:
        return self._read_failures.pop(path, None) is not None

    def evidence_failure(self) -> EvidenceFailure | None:
        if self._read_failures:
            return self._read_failures[min(self._read_failures, key=str)]
        failed = next((r for r in self._bash if r.is_tracked and r.execution_error), None)
        if failed:
            return EvidenceFailure(
                code="pi_bash_execution_unresolved", detail=failed.execution_error
            )
        if self._unobserved_messages:
            return EvidenceFailure(
                code="pi_delivery_event_unobserved",
                detail=", ".join(sorted(self._unobserved_messages)),
            )
        return None

    def blocker_snapshot(
        self,
        *,
        correlated_bash_ids: frozenset[str] = frozenset(),
    ) -> PiPrivateWorkSnapshot:
        blockers: list[PiPrivateWorkBlocker] = []
        for record in self._bash:
            if not record.is_tracked or not record.is_background:
                continue
            if record.status == "running":
                blockers.append(
                    PiPrivateWorkBlocker("tracked_bash", "pi_tracked_bash_bg", record.bash_id)
                )
            elif (
                record.bash_id not in correlated_bash_ids
                and record.bash_id not in self._consumed
                and record.notification_consumed_at_ms is None
            ):
                blockers.append(
                    PiPrivateWorkBlocker(
                        "disk_notification", "pi_result_delivery_pending", record.bash_id
                    )
                )
        blockers.extend(
            PiPrivateWorkBlocker("disk_notification", "pi_delivery_event_pending", identity)
            for identity in sorted(self._unobserved_messages)
        )
        return PiPrivateWorkSnapshot(
            tracked_bash_bg=any(b.kind == "tracked_bash" for b in blockers),
            pending_disk_notification=any(b.kind == "disk_notification" for b in blockers),
            blockers=tuple(blockers),
            failure=self.evidence_failure(),
            consumed_work_ids=self._consumed,
        )
