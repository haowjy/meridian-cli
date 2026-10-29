"""Post-execution extraction pipeline used during run finalization."""

from enum import StrEnum
from functools import partial
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.native_identity import NativeKey
from meridian.lib.core.spawn_lifecycle import (
    DurableReportEvidence,
    classify_durable_report_text,
)
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SpawnExtractor
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.cost import estimate_usage_cost
from meridian.lib.launch.report import ExtractedReport, extract_or_fallback_report
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.permission_journal import PermissionJournal

# ---------------------------------------------------------------------------
# Finalization pipeline
# ---------------------------------------------------------------------------


class FinalizeReportKind(StrEnum):
    """Extraction-boundary classification for the persisted final report."""

    ABSENT = "absent"
    DURABLE_COMPLETION = "durable_completion"
    SYNTHETIC_FAILURE = "synthetic_failure"
    PI_FAILURE = "pi_failure"
    CONTROL_FRAME = "control_frame"


class FinalizeExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    usage: TokenUsage | None
    harness_session_id: str | None
    report_path: Path | None
    report: ExtractedReport
    output_is_empty: bool
    report_kind: FinalizeReportKind = FinalizeReportKind.ABSENT

    @property
    def durable_report_completion(self) -> bool:
        return self.report_kind is FinalizeReportKind.DURABLE_COMPLETION


def _persist_report(
    *,
    launch: LaunchPaths,
    extracted: ExtractedReport,
    text_capped: bool,
) -> Path | None:
    if extracted.content is None:
        return None

    target = launch.report
    if extracted.source in {"assistant_message", "failure_reason", "pi_failure"}:
        heading = (
            "# Spawn failed" if extracted.source in {"failure_reason", "pi_failure"} else "# Report"
        )
        wrapped = f"{heading}\n\n{extracted.content.strip()}\n"
        if text_capped and extracted.source == "assistant_message":
            wrapped += "\n[Attempt text was truncated at 1 MiB before report extraction.]\n"
        atomic_write_text(target, wrapped)
        return target

    # The harness wrote report.md directly; its spawn-local path is authoritative.
    return target


def classify_finalize_report(extracted: ExtractedReport) -> FinalizeReportKind:
    """Project extracted report source and text into finalization semantics."""

    if extracted.source == "failure_reason":
        return FinalizeReportKind.SYNTHETIC_FAILURE
    if extracted.source == "pi_failure":
        return FinalizeReportKind.PI_FAILURE

    evidence = classify_durable_report_text(extracted.content)
    if evidence is DurableReportEvidence.COMPLETION:
        return FinalizeReportKind.DURABLE_COMPLETION
    if evidence is DurableReportEvidence.SYNTHETIC_FAILURE:
        return FinalizeReportKind.SYNTHETIC_FAILURE
    if evidence is DurableReportEvidence.CONTROL_FRAME:
        return FinalizeReportKind.CONTROL_FRAME
    return FinalizeReportKind.ABSENT


def enrich_finalize(
    *,
    extractor: SpawnExtractor,
    facts: AttemptFacts,
    native_key: NativeKey | None = None,
    launch: LaunchPaths,
    model_id: str | None = None,
    harness_id: HarnessId | str | None = None,
    project_root: Path | None = None,
    failure_reason: str | None = None,
) -> FinalizeExtraction:
    """Spawn all extraction steps and return one enriched finalization payload."""

    if facts.incomplete:
        structlog.get_logger(__name__).warning("facts_incomplete", spawn_id=str(launch.spawn_id))
    usage = (
        estimate_usage_cost(
            model_id=(model_id or "").strip() or None,
            usage=facts.usage,
            project_root=project_root,
            harness_id=str(harness_id) if harness_id is not None else None,
        )
        if facts.usage is not None
        else None
    )
    harness_session_id = facts.first_session_id
    if facts.permission_denials:
        journal = PermissionJournal(launch)
        for denial in facts.permission_denials:
            journal.record_denial(denial)
    report = extract_or_fallback_report(
        launch,
        facts=facts,
        load_native_text=partial(extractor.read_native_turn, native_key, facts.native_turn_ids)
        if native_key is not None
        else None,
        failure_reason=failure_reason,
    )
    report_path = _persist_report(
        launch=launch,
        extracted=report,
        text_capped=facts.report_text_capped,
    )

    return FinalizeExtraction(
        usage=usage,
        harness_session_id=harness_session_id,
        report_path=report_path,
        report=report,
        output_is_empty=not (facts.output_seen or (report.content and report.content.strip())),
        report_kind=classify_finalize_report(report),
    )
