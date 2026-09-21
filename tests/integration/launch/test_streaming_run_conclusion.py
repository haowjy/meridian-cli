"""Streaming conclusion behavior that crosses the artifact boundary."""

from pathlib import Path

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.spawn_lifecycle import (
    ExecutionTerminalFacts,
    resolve_execution_terminal_outcome,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.launch.constants import HISTORY_FILENAME, REPORT_FILENAME
from meridian.lib.launch.extract import FinalizeReportKind, enrich_finalize
from meridian.lib.launch.streaming_runner import StreamingRunConclusion
from meridian.lib.state.artifact_store import InMemoryStore, make_artifact_key

# p6491: the OpenCode adapter extracted the agent's streamed narration preamble as
# the "report"; no turn ever completed.
_PARTIAL_NARRATION_REPORT = (
    "# Report\n\n"
    "I'll start by orienting myself: confirming the host, OS, and available tooling.\n"
)

# p6493: the extracted "report" is the raw `permission.asked` event envelope.
_PERMISSION_ENVELOPE_REPORT = (
    "# Report\n\n"
    '{"id":"evt_0c1ff9c95002GNKt2zAgnJbwN2","properties":{'
    '"always":["/home/jimyao/.meridian/ref/opencode/*"],'
    '"permission":"external_directory","sessionID":"ses_f3e0158b0ffeXc8CfYD1e0pKPR"},'
    '"type":"permission.asked"}\n'
)

# One `permission.asked`, zero `turn.completed` — the hang shape.
_PERMISSION_HISTORY = (
    '{"event_type":"permission.asked","timestamp":"2026-09-21T03:25:57.100Z",'
    '"payload":{"properties":{"permission":"external_directory",'
    '"patterns":["/home/jimyao/.meridian/ref/opencode/*"]}}}\n'
)


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


def _seed_spawn(artifacts: InMemoryStore, spawn_id: SpawnId, *, report_text: str) -> None:
    artifacts.put(
        make_artifact_key(spawn_id, HISTORY_FILENAME),
        _PERMISSION_HISTORY.encode("utf-8"),
    )
    artifacts.put(
        make_artifact_key(spawn_id, REPORT_FILENAME),
        report_text.encode("utf-8"),
    )


def test_hung_permission_spawn_resolves_cancelled_not_succeeded(tmp_path: Path) -> None:
    """p6491/p6493: a SIGTERM'd permission-hang is not a report-based success."""

    spawn_id = SpawnId("p-hung-permission")
    artifacts = InMemoryStore()
    _seed_spawn(artifacts, spawn_id, report_text=_PARTIAL_NARRATION_REPORT)

    extraction = enrich_finalize(
        artifacts=artifacts,
        extractor=_NoReportExtractor(),
        spawn_id=spawn_id,
        log_dir=tmp_path,
        failure_reason="terminated",
    )

    facts = ExecutionTerminalFacts(
        exit_code=143,
        failure_reason="terminated",
        cancellation_observed=True,
        durable_report_completion=extraction.durable_report_completion,
    )
    outcome = resolve_execution_terminal_outcome(facts)

    assert (outcome.status, outcome.exit_code) == ("cancelled", 143)


def test_permission_envelope_report_is_not_durable_completion(tmp_path: Path) -> None:
    """p6493: a raw harness event envelope must not classify as completion."""

    spawn_id = SpawnId("p-permission-envelope")
    artifacts = InMemoryStore()
    _seed_spawn(artifacts, spawn_id, report_text=_PERMISSION_ENVELOPE_REPORT)

    extraction = enrich_finalize(
        artifacts=artifacts,
        extractor=_NoReportExtractor(),
        spawn_id=spawn_id,
        log_dir=tmp_path,
        failure_reason="terminated",
    )

    assert extraction.report_kind is FinalizeReportKind.ABSENT
    assert extraction.durable_report_completion is False


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
