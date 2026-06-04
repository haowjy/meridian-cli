from pathlib import Path

from meridian.lib.state.spawn_report import spawn_report_has_durable_completion


def test_spawn_report_has_durable_completion_reads_report_from_spawn_dir(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "s-test"
    spawn_dir.mkdir(parents=True)
    (spawn_dir / "report.md").write_text("# Report\n\nDone.\n", encoding="utf-8")

    assert spawn_report_has_durable_completion(tmp_path, "s-test") is True
    assert spawn_report_has_durable_completion(tmp_path, "missing") is False


def test_spawn_report_rejects_wrapped_codex_close_error_control_payload(
    tmp_path: Path,
) -> None:
    spawn_dir = tmp_path / "spawns" / "s-test"
    spawn_dir.mkdir(parents=True)
    (spawn_dir / "report.md").write_text(
        '# Report\n\n{"type":"error","message":"no close frame received or sent"}\n',
        encoding="utf-8",
    )

    assert spawn_report_has_durable_completion(tmp_path, "s-test") is False
