"""File-backed characterization of reconciled descendant evidence."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.ops.session_archive import archive_history
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.streaming import descendant_evidence as descendant_evidence_module
from meridian.lib.streaming.descendant_evidence import ReconciledDescendantEvidence
from tests.support.history import warm_history_index
from tests.support.resident_drain import start_row

if TYPE_CHECKING:
    import pytest

    from meridian.lib.state.history_changes import DirtySource
    from meridian.lib.state.history_index import IndexCoverage


def test_reconciled_descendant_evidence_is_transitive_and_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    start_row(tmp_path, "p3", HarnessId.CODEX, "p2")
    descendant_evidence_module.spawn_store.finalize_spawn(
        tmp_path,
        SpawnId("p2"),
        "succeeded",
        0,
        origin="runner",
    )
    descendant_evidence_module.spawn_store.mark_finalizing(tmp_path, SpawnId("p3"))
    warm_history_index(tmp_path)

    evidence = ReconciledDescendantEvidence(
        runtime_root=tmp_path,
        root_spawn_id=SpawnId("p1"),
    )
    assessment = evidence.assess()

    assert assessment.disposition == "blocked"
    assert assessment.blockers == (
        descendant_evidence_module.DiagnosticBlocker(
            source="persisted_descendant",
            code="active_descendant",
            identity="p3",
        ),
    )

    for spawn_id in ("p10", "p11", "p12"):
        start_row(tmp_path, spawn_id, HarnessId.CODEX, None)
        descendant_evidence_module.spawn_store.finalize_spawn(
            tmp_path, SpawnId(spawn_id), "succeeded", 0, origin="runner"
        )
    reads: list[str] = []
    real_get_spawn = descendant_evidence_module.spawn_store.get_spawn

    def tracked_get_spawn(runtime_root: Path, spawn_id: SpawnId | str):  # type: ignore[no-untyped-def]
        reads.append(str(spawn_id))
        return real_get_spawn(runtime_root, spawn_id)

    monkeypatch.setattr(descendant_evidence_module.spawn_store, "get_spawn", tracked_get_spawn)
    warm = evidence.assess()
    assert warm.disposition == "blocked"
    assert [blocker.identity for blocker in warm.blockers] == ["p3"]
    assert reads == ["p2", "p3"]


def test_cold_build_gets_a_separate_descendant_query_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    index = HistoryIndex(tmp_path)
    assert not index.path.exists()

    monkeypatch.setattr(descendant_evidence_module, "QUERY_TIMEOUT", 0.2)
    original_rebuild = HistoryIndex._rebuild_locked
    slowed: list[float] = []

    def slow_rebuild(
        self: HistoryIndex, *, reset: bool, deadline: float
    ) -> tuple[IndexCoverage, list[DirtySource]]:
        # Cold build outlasts the query budget, independent of machine load.
        slowed.append(0.4)
        time.sleep(0.4)
        return original_rebuild(self, reset=reset, deadline=deadline)

    monkeypatch.setattr(HistoryIndex, "_rebuild_locked", slow_rebuild)

    projection = ReconciledDescendantEvidence(
        runtime_root=tmp_path,
        root_spawn_id=SpawnId("p1"),
    ).project()

    assert slowed, "cold build path was not exercised"
    assert index.path.exists()
    assert projection.assessment.disposition == "blocked", projection.assessment
    assert [blocker.identity for blocker in projection.assessment.blockers] == ["p2"]


def test_reconciled_descendant_evidence_reports_invalid_rows_as_unknown(
    tmp_path: Path,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "unrelated")
    invalid_state = tmp_path / "spawns" / "p3" / "state.json"
    invalid_state.parent.mkdir(parents=True)
    atomic_write_text(invalid_state, "not json")

    assessment = ReconciledDescendantEvidence(
        runtime_root=tmp_path,
        root_spawn_id=SpawnId("p1"),
    ).assess()

    assert assessment.disposition == "unknown"
    assert assessment.blockers == ()
    assert assessment.failure is not None
    assert assessment.failure.code == "descendant_evidence_read_failed"
    assert str(invalid_state) in assessment.failure.detail
    assert "meridian doctor" not in assessment.failure.detail


def test_reconciled_descendant_evidence_returns_typed_unknown_on_store_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_projection(
        _index: object,
        _root_spawn_id: str,
        *,
        deadline: float | None = None,
    ) -> object:
        assert deadline is not None
        raise OSError("store unavailable")

    monkeypatch.setattr(
        descendant_evidence_module.HistoryIndex,
        "descendant_projection",
        _fail_projection,
    )

    assessment = ReconciledDescendantEvidence(
        runtime_root=tmp_path,
        root_spawn_id=SpawnId("p1"),
    ).assess()

    assert assessment.disposition == "unknown"
    assert assessment.blockers == ()
    assert assessment.failure == descendant_evidence_module.EvidenceFailure(
        code="descendant_evidence_read_failed",
        detail="store unavailable",
    )


def test_reconciled_descendant_evidence_treats_busy_index_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadlines: list[float] = []

    def _busy_projection(
        _index: object,
        _root_spawn_id: str,
        *,
        deadline: float | None = None,
    ) -> object:
        assert deadline is not None
        deadlines.append(deadline)
        raise descendant_evidence_module.HistoryIndexBusy("index busy")

    monkeypatch.setattr(
        descendant_evidence_module.HistoryIndex,
        "descendant_projection",
        _busy_projection,
    )
    before = descendant_evidence_module.time.monotonic()

    assessment = ReconciledDescendantEvidence(
        runtime_root=tmp_path,
        root_spawn_id=SpawnId("p1"),
    ).assess()

    assert assessment.disposition == "unknown"
    assert assessment.blockers == ()
    assert assessment.failure == descendant_evidence_module.EvidenceFailure(
        code="descendant_evidence_unavailable",
        detail="index busy",
    )
    assert before + descendant_evidence_module.QUERY_TIMEOUT <= deadlines[0]
    assert deadlines[0] <= (
        descendant_evidence_module.time.monotonic()
        + descendant_evidence_module.QUERY_TIMEOUT
    )


def test_archived_intermediate_preserves_live_grandchild_membership(tmp_path: Path) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    descendant_evidence_module.spawn_store.finalize_spawn(
        tmp_path, SpawnId("p2"), "succeeded", 0, origin="runner"
    )
    descendant_evidence_module.spawn_store.finalize_spawn(
        tmp_path, SpawnId("p1"), "succeeded", 0, origin="runner"
    )
    archive_history(
        tmp_path,
        destination=tmp_path.parent / f"{tmp_path.name}-archives",
        refs=("p2",),
        apply=True,
    )
    start_row(tmp_path, "p3", HarnessId.CODEX, "p2")
    descendant_evidence_module.spawn_store.mark_finalizing(tmp_path, SpawnId("p3"))
    warm_history_index(tmp_path)
    evidence = ReconciledDescendantEvidence(
        runtime_root=tmp_path,
        root_spawn_id=SpawnId("p1"),
    )
    assert evidence.assess().disposition == "blocked"
    assessment = evidence.assess()

    assert assessment.disposition == "blocked"
    assert [blocker.identity for blocker in assessment.blockers] == ["p3"]


def test_descendant_projection_stops_when_indexed_parent_edges_cycle(tmp_path: Path) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, "p2")
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")

    assert HistoryIndex(tmp_path).descendant_projection(("p1",)) == (
        ("p2", "p1", False),
    )
