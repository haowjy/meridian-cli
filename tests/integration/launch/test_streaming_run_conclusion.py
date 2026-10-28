"""Streaming conclusion behavior that crosses the artifact boundary."""

from pathlib import Path

from meridian.lib.core.spawn_lifecycle import (
    ExecutionTerminalFacts,
    resolve_execution_terminal_outcome,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.extractors.codex import CODEX_EXTRACTOR
from meridian.lib.launch.extract import FinalizeReportKind, enrich_finalize
from meridian.lib.launch.streaming_runner import StreamingRunConclusion
from meridian.lib.state.paths import LaunchPaths

# p6491: the OpenCode adapter extracted the agent's streamed narration preamble as
# the "report"; no turn ever completed.
_PARTIAL_NARRATION_REPORT = (
    "# Report\n\nI'll start by orienting myself: confirming the host, OS, and available tooling.\n"
)

# p6493: the extracted "report" is the raw `permission.asked` event envelope.
_PERMISSION_ENVELOPE_REPORT = (
    "# Report\n\n"
    '{"id":"evt_0c1ff9c95002GNKt2zAgnJbwN2","properties":{'
    '"always":["/home/jimyao/.meridian/ref/opencode/*"],'
    '"permission":"external_directory","sessionID":"ses_f3e0158b0ffeXc8CfYD1e0pKPR"},'
    '"type":"permission.asked"}\n'
)

def _seed_report(spawn_dir: Path, *, report_text: str) -> None:
    (LaunchPaths(spawn_dir).report).write_text(report_text, encoding="utf-8")


def test_hung_permission_spawn_resolves_cancelled_not_succeeded(tmp_path: Path) -> None:
    """p6491/p6493: a SIGTERM'd permission-hang is not a report-based success."""

    spawn_id = SpawnId("p-hung-permission")
    _seed_report(tmp_path, report_text=_PARTIAL_NARRATION_REPORT)

    extraction = enrich_finalize(
        extractor=CODEX_EXTRACTOR,
        facts=AttemptFacts(),
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
    _seed_report(tmp_path, report_text=_PERMISSION_ENVELOPE_REPORT)

    extraction = enrich_finalize(
        extractor=CODEX_EXTRACTOR,
        facts=AttemptFacts(),
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

    extraction = enrich_finalize(
        extractor=CODEX_EXTRACTOR,
        facts=AttemptFacts(),
        spawn_id=spawn_id,
        log_dir=tmp_path,
        failure_reason="Cursor subprocess exited with code 130.",
    )

    report = (LaunchPaths(tmp_path).report).read_text(encoding="utf-8")
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
