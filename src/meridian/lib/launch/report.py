"""Report precedence over attempt-local facts and exact native replies."""

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.spawn_lifecycle import DurableReportEvidence, classify_durable_report_text
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.state.artifact_store import ArtifactStore

from .artifact_io import read_artifact_text

ReportSource = Literal["report_md", "assistant_message", "failure_reason", "pi_failure"]


class ExtractedReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str | None
    source: ReportSource | None


def _is_terminal_control_frame(text: str) -> bool:
    return classify_durable_report_text(text) is DurableReportEvidence.CONTROL_FRAME


def extract_or_fallback_report(
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    *,
    facts: AttemptFacts,
    load_native_text: Callable[[], str | None] | None = None,
    failure_reason: str | None = None,
) -> ExtractedReport:
    report = read_artifact_text(artifacts, spawn_id, "report.md").strip()
    if report and not _is_terminal_control_frame(report):
        return ExtractedReport(content=report, source="report_md")
    if facts.failure is not None:
        return ExtractedReport(content=facts.failure.message, source="pi_failure")
    native_text = load_native_text() if load_native_text is not None else None
    text = (native_text or facts.final_text or "").strip()
    if text and not _is_terminal_control_frame(text):
        return ExtractedReport(content=text, source="assistant_message")
    if failure_reason and failure_reason.strip():
        return ExtractedReport(content=failure_reason.strip(), source="failure_reason")
    return ExtractedReport(content=None, source=None)
