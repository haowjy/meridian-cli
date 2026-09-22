"""Shared reconciled, transitive persisted-descendant evidence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.state import spawn_store
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.reaper import peek_reconciled_active_spawn
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.streaming.completion_contracts import (
    DiagnosticBlocker,
    EvidenceFailure,
    WorkAssessment,
)

logger = logging.getLogger(__name__)


class ReconciledDescendantEvidence:
    """Project valid spawn rows into active transitive descendant blockers."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        root_spawn_id: SpawnId,
        blocker_reader: Callable[[], tuple[DiagnosticBlocker, ...]] | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._root_spawn_id = root_spawn_id
        self._blocker_reader = blocker_reader

    def assess(self) -> WorkAssessment:
        """Return a fresh blocker assessment, or typed unknown on a hard read failure."""

        try:
            if self._blocker_reader is None:
                blockers = self._read_blockers()
            else:
                blockers = self._blocker_reader()
        except Exception as exc:
            failure = EvidenceFailure(code="descendant_evidence_read_failed", detail=str(exc))
            return WorkAssessment(
                disposition="unknown",
                blockers=(),
                generation=0,
                failure=failure,
            )

        if blockers:
            return WorkAssessment(
                disposition="blocked",
                blockers=blockers,
                generation=0,
            )
        return WorkAssessment(disposition="ready", blockers=(), generation=0)

    def _read_blockers(
        self,
    ) -> tuple[DiagnosticBlocker, ...]:
        selected = HistoryIndex(self._runtime_root).descendant_projection(
            str(self._root_spawn_id)
        )
        descendants: list[SpawnRecord] = []
        for spawn_id, _parent_id, archived in selected:
            if archived:
                continue
            row = spawn_store.get_spawn(self._runtime_root, spawn_id)
            if row is None:
                raise RuntimeError(f"indexed descendant state is missing: {spawn_id}")
            descendants.append(peek_reconciled_active_spawn(self._runtime_root, row))
        blockers = tuple(
            DiagnosticBlocker(
                source="persisted_descendant",
                code="active_descendant",
                identity=row.id,
            )
            for row in descendants
            if row.id != str(self._root_spawn_id) and is_active_spawn_status(row.status)
        )
        return blockers



@dataclass(frozen=True)
class RefreshSnapshot:
    assessment: WorkAssessment
    completed_request: int


class DescendantRefreshOwner:
    """Single-flight, finish-scheduled owner for descendant evidence reads."""

    def __init__(
        self,
        evidence: ReconciledDescendantEvidence,
        *,
        poll_seconds: float,
        clock: Callable[[], float],
        on_commit: Callable[[WorkAssessment], None] | None = None,
    ) -> None:
        self._evidence = evidence
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._on_commit = on_commit
        self._snapshot: RefreshSnapshot | None = None
        self._task: asyncio.Task[WorkAssessment] | None = None
        self._task_request = 0
        self._requested = 0
        self._next_due: float | None = None
        self._wakes: asyncio.Queue[None] = asyncio.Queue()
        self._closed = True
        self._generation = 0
        self._signature: object = None
        self._epoch = 0

    @property
    def assessment(self) -> WorkAssessment:
        if self._snapshot is not None:
            return self._snapshot.assessment
        return WorkAssessment(
            disposition="unknown", blockers=(), generation=0,
            failure=EvidenceFailure(code="descendant_evidence_unavailable"),
        )

    def start(self) -> None:
        if not self._closed:
            return
        self._closed = False
        self._epoch += 1
        self._schedule()

    async def stop(self) -> None:
        self._closed = True
        self._epoch += 1
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._next_due = None
        self._wakes.put_nowait(None)

    def request(self) -> int:
        if self._closed:
            self.start()
        self._requested += 1
        self._schedule()
        return self._requested

    def ensure_due(self) -> None:
        if self._closed:
            self.start()
        if self._next_due is None or self._clock() >= self._next_due:
            self._schedule()

    def validated(self, request: int) -> bool:
        return self._snapshot is not None and self._snapshot.completed_request >= request

    @property
    def next_due_at(self) -> float | None:
        return self._next_due

    @property
    def wants_wake(self) -> bool:
        return not self._closed

    async def wait(self) -> None:
        await self._wakes.get()

    def _schedule(self) -> None:
        if self._closed or self._task is not None:
            return
        self._next_due = None
        self._task_request = self._requested
        epoch = self._epoch
        task = asyncio.create_task(asyncio.to_thread(self._evidence.assess))
        self._task = task
        task.add_done_callback(lambda done: self._commit(done, epoch))

    def _commit(self, task: asyncio.Task[WorkAssessment], epoch: int) -> None:
        if self._closed or epoch != self._epoch or task is not self._task:
            return
        self._task = None
        try:
            raw = task.result()
        except BaseException as exc:
            raw = WorkAssessment(
                disposition="unknown", blockers=(), generation=0,
                failure=EvidenceFailure(code="descendant_evidence_read_failed", detail=str(exc)),
            )
        signature = (raw.disposition, raw.blockers, raw.failure)
        if signature != self._signature:
            self._generation += 1
            self._signature = signature
        assessment = WorkAssessment(
            disposition=raw.disposition,
            blockers=raw.blockers,
            generation=self._generation,
            failure=raw.failure,
        )
        self._snapshot = RefreshSnapshot(assessment, self._task_request)
        self._next_due = self._clock() + self._poll_seconds
        if self._on_commit is not None:
            try:
                self._on_commit(assessment)
            except Exception:
                # Policy callback failures cannot strand the owner or suppress
                # the completion wake; the coordinator will still reevaluate.
                logger.exception("Descendant refresh commit callback failed")
        self._wakes.put_nowait(None)
        if self._requested > self._task_request:
            self._schedule()


__all__ = ["DescendantRefreshOwner", "ReconciledDescendantEvidence"]
