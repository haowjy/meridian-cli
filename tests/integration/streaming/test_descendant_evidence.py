"""File-backed characterization of reconciled descendant evidence."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.streaming import descendant_evidence as descendant_evidence_module
from meridian.lib.streaming.descendant_evidence import ReconciledDescendantEvidence
from tests.support.resident_drain import start_row

if TYPE_CHECKING:
    import pytest


def test_reconciled_descendant_evidence_is_transitive_and_reconciled(
    tmp_path: Path,
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
    assert evidence.persisted_descendant_ids == ("p2", "p3")


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
    assert "Spawn state quarantined" in assessment.failure.detail


def test_reconciled_descendant_evidence_returns_typed_unknown_on_store_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_list_spawns(_runtime_root: Path) -> object:
        raise OSError("store unavailable")

    monkeypatch.setattr(
        descendant_evidence_module.spawn_store,
        "list_spawns",
        _fail_list_spawns,
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
