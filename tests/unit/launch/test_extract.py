from pathlib import Path

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.launch.extract import _persist_report
from meridian.lib.launch.report import ExtractedReport
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
