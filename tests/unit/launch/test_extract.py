from pathlib import Path

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.launch.extract import (
    FinalizeReportKind,
    _persist_report,
    classify_finalize_report,
)
from meridian.lib.launch.report import ExtractedReport, extract_or_fallback_report
from meridian.lib.state.artifact_store import LocalStore


def test_persist_report_wraps_assistant_extract_with_report_heading(tmp_path: Path) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-extract")
    log_dir = tmp_path / "spawns" / str(spawn_id)
    log_dir.mkdir(parents=True, exist_ok=True)

    report_path = _persist_report(
        artifacts=artifacts,
        spawn_id=spawn_id,
        log_dir=log_dir,
        extracted=ExtractedReport(content="Done.", source="assistant_message"),
        secrets=(),
    )

    assert report_path == log_dir / "report.md"
    expected = "# Report\n\nDone.\n"
    assert report_path.read_text(encoding="utf-8") == expected
    assert artifacts.get(ArtifactKey(f"{spawn_id}/report.md")).decode("utf-8") == expected


def test_classify_finalize_report_rejects_codex_close_error_payload() -> None:
    report = ExtractedReport(
        content='{"type":"error/connectionClosed","message":"no close frame received or sent"}',
        source="assistant_message",
    )

    assert classify_finalize_report(report) is FinalizeReportKind.CONTROL_FRAME


def test_classify_finalize_report_keeps_genuine_json_completion() -> None:
    report = ExtractedReport(content='{"message":"Done."}', source="assistant_message")

    assert classify_finalize_report(report) is FinalizeReportKind.DURABLE_COMPLETION


def test_extract_or_fallback_report_ignores_codex_connection_closed_history(
    tmp_path: Path,
) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-codex-close")
    artifacts.put(
        ArtifactKey(f"{spawn_id}/history.jsonl"),
        b'{"event_type":"error/connectionClosed",'
        b'"payload":{"message":"no close frame received or sent"},'
        b'"seq":5}\n',
    )

    report = extract_or_fallback_report(artifacts, spawn_id)

    assert report.content is None
    assert report.source is None
