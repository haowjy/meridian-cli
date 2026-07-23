"""Streaming conclusion behavior that crosses the artifact boundary."""

from pathlib import Path

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.launch.constants import REPORT_FILENAME
from meridian.lib.launch.extract import FinalizeReportKind, enrich_finalize
from meridian.lib.launch.streaming_runner import StreamingRunConclusion
from meridian.lib.state.artifact_store import InMemoryStore, make_artifact_key


class _NoReportExtractor:
    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        _ = artifacts, spawn_id
        return TokenUsage()

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        return None

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        return None


def test_enrich_finalize_marks_synthetic_failure_report_not_durable(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p-cancel-failure-report")
    artifacts = InMemoryStore()

    extraction = enrich_finalize(
        artifacts=artifacts,
        extractor=_NoReportExtractor(),
        spawn_id=spawn_id,
        log_dir=tmp_path,
        failure_reason="Cursor subprocess exited with code 130.",
    )

    report = artifacts.get(make_artifact_key(spawn_id, REPORT_FILENAME)).decode()
    conclusion = StreamingRunConclusion(
        exit_code=130,
        failure_reason="cancelled",
        extracted=extraction,
        cancellation_observed=True,
    )

    assert report.startswith("# Spawn failed")
    assert extraction.report_kind is FinalizeReportKind.SYNTHETIC_FAILURE
    assert extraction.durable_report_completion is False
    assert conclusion.terminal_facts(received_signal=None).durable_report_completion is False
