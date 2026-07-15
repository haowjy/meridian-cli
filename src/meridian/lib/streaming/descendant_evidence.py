"""Shared reconciled, transitive persisted-descendant evidence."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.state import spawn_store
from meridian.lib.state.reaper import peek_reconciled_active_spawn
from meridian.lib.state.spawn_tree import collect_descendants
from meridian.lib.streaming.completion_contracts import (
    DiagnosticBlocker,
    EvidenceFailure,
    WorkAssessment,
)


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
        self._blocker_reader = blocker_reader or self._read_blockers
        self._generation = 0
        self._last_signature: object = None

    def assess(self) -> WorkAssessment:
        """Return a fresh blocker assessment, or typed unknown on a hard read failure."""

        try:
            blockers = self._blocker_reader()
        except Exception as exc:
            failure = EvidenceFailure(code="descendant_evidence_read_failed", detail=str(exc))
            return WorkAssessment(
                disposition="unknown",
                blockers=(),
                generation=self._next_generation(("unknown", failure.code, failure.detail)),
                failure=failure,
            )

        signature = tuple((blocker.code, blocker.identity) for blocker in blockers)
        generation = self._next_generation(signature)
        if blockers:
            return WorkAssessment(
                disposition="blocked",
                blockers=blockers,
                generation=generation,
            )
        return WorkAssessment(disposition="ready", blockers=(), generation=generation)

    def _read_blockers(self) -> tuple[DiagnosticBlocker, ...]:
        rows = [
            peek_reconciled_active_spawn(self._runtime_root, row)
            for row in spawn_store.list_spawns(self._runtime_root)
        ]
        return tuple(
            DiagnosticBlocker(
                source="persisted_descendant",
                code="active_descendant",
                identity=row.id,
            )
            for row in collect_descendants(str(self._root_spawn_id), rows)
            if row.id != str(self._root_spawn_id) and is_active_spawn_status(row.status)
        )

    def _next_generation(self, signature: object) -> int:
        if signature != self._last_signature:
            self._generation += 1
            self._last_signature = signature
        return self._generation


__all__ = ["ReconciledDescendantEvidence"]
