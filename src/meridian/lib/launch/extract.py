"""Post-execution extraction pipeline used during run finalization."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import ArtifactKey, HarnessId, SpawnId
from meridian.lib.harness.adapter import SpawnExtractor
from meridian.lib.harness.cost import estimate_usage_cost
from meridian.lib.launch.artifact_io import read_artifact_text
from meridian.lib.launch.constants import (
    HISTORY_FILENAME,
    OUTPUT_FILENAME,
    REPORT_FILENAME,
    STDERR_FILENAME,
    TOKENS_FILENAME,
)
from meridian.lib.launch.report import ExtractedReport, extract_or_fallback_report
from meridian.lib.safety.redaction import SecretSpec, redact_secrets
from meridian.lib.state.artifact_store import ArtifactStore
from meridian.lib.state.atomic import atomic_write_text

# ---------------------------------------------------------------------------
# Finalization pipeline
# ---------------------------------------------------------------------------


class FinalizeExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    usage: TokenUsage
    harness_session_id: str | None
    report_path: Path | None
    report: ExtractedReport
    output_is_empty: bool


def reset_finalize_attempt_artifacts(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    log_dir: Path,
) -> None:
    """Clear attempt-scoped artifacts so retries never reuse stale extraction state."""

    for name in (OUTPUT_FILENAME, STDERR_FILENAME, TOKENS_FILENAME, REPORT_FILENAME):
        artifacts.delete(ArtifactKey(f"{spawn_id}/{name}"))

    report_path = log_dir / REPORT_FILENAME
    if report_path.exists():
        report_path.unlink()


def _persist_report(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    log_dir: Path,
    extracted: ExtractedReport,
    secrets: tuple[SecretSpec, ...],
) -> Path | None:
    if extracted.content is None:
        return None

    redacted_content = redact_secrets(extracted.content, secrets)
    target = log_dir / REPORT_FILENAME
    report_key = ArtifactKey(f"{spawn_id}/{REPORT_FILENAME}")
    if extracted.source in {"assistant_message", "failure_reason", "pi_failure"}:
        heading = "# Spawn failed" if extracted.source in {"failure_reason", "pi_failure"} else "# Report"
        wrapped = f"{heading}\n\n{redacted_content.strip()}\n"
        atomic_write_text(target, wrapped)
        artifacts.put(report_key, wrapped.encode("utf-8"))
        return target

    # The harness may have written report.md directly. Ensure both filesystem and artifact
    # views are populated so downstream readers can consume a single source.
    text = redacted_content
    atomic_write_text(target, text)
    artifacts.put(report_key, text.encode("utf-8"))
    return target


def _is_empty_output(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    extracted_report: ExtractedReport,
) -> bool:
    if extracted_report.content and extracted_report.content.strip():
        return False
    history_text = read_artifact_text(artifacts, spawn_id, HISTORY_FILENAME)
    if history_text.strip():
        return False
    output_text = read_artifact_text(artifacts, spawn_id, OUTPUT_FILENAME)
    return not output_text.strip()


def enrich_finalize(
    *,
    artifacts: ArtifactStore,
    extractor: SpawnExtractor,
    spawn_id: SpawnId,
    log_dir: Path,
    model_id: str | None = None,
    harness_id: HarnessId | str | None = None,
    project_root: Path | None = None,
    secrets: tuple[SecretSpec, ...] = (),
    failure_reason: str | None = None,
) -> FinalizeExtraction:
    """Spawn all extraction steps and return one enriched finalization payload."""

    usage = estimate_usage_cost(
        model_id=(model_id or "").strip() or None,
        usage=extractor.extract_usage(artifacts, spawn_id),
        project_root=project_root,
        harness_id=str(harness_id) if harness_id is not None else None,
    )
    harness_session_id = extractor.extract_session_id(artifacts, spawn_id)
    report = extract_or_fallback_report(
        artifacts,
        spawn_id,
        extractor=extractor,
        failure_reason=failure_reason,
    )
    report_path = _persist_report(
        artifacts=artifacts,
        spawn_id=spawn_id,
        log_dir=log_dir,
        extracted=report,
        secrets=secrets,
    )

    return FinalizeExtraction(
        usage=usage,
        harness_session_id=harness_session_id,
        report_path=report_path,
        report=report,
        output_is_empty=_is_empty_output(
            artifacts=artifacts,
            spawn_id=spawn_id,
            extracted_report=report,
        ),
    )
