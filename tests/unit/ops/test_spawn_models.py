"""Unit tests for spawn output model formatting and wire contracts."""

from __future__ import annotations

from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnDetailOutput,
    SpawnWaitMultiOutput,
)

# qa-validated: spawn-return-report


def _make_spawn_detail(
    *,
    spawn_id: str = "p1",
    status: str = "succeeded",
    duration_secs: float | None = None,
    exit_code: int | None = None,
    failure_reason: str | None = None,
    report_body: str | None = "done report",
) -> SpawnDetailOutput:
    return SpawnDetailOutput(
        spawn_id=spawn_id,
        status=status,
        model="gpt-5.4",
        harness="codex",
        started_at="2026-05-15T00:00:00Z",
        finished_at="2026-05-15T00:00:04Z",
        duration_secs=duration_secs,
        exit_code=exit_code,
        failure_reason=failure_reason,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        report_path="/tmp/report.md",
        report_summary=report_body,
        report_body=report_body,
    )


def test_spawn_action_terminal_success_compact_text_exact() -> None:
    output = SpawnActionOutput(
        command="spawn.create",
        status="succeeded",
        spawn_id="p1",
        report="body",
        duration_secs=1.2,
        exit_code=0,
    )

    text = output.format_text()
    assert text == "p1 succeeded (1.2s)\n\nbody\n\nTranscript: meridian session log p1"
    assert "Spawn id:" not in text
    assert "Model:" not in text
    assert "Exit code:" not in text


def test_spawn_action_terminal_success_no_report_keeps_transcript() -> None:
    output = SpawnActionOutput(command="spawn.create", status="succeeded", spawn_id="p2")

    assert output.format_text() == (
        "p2 succeeded\n\n(no report)\n\nTranscript: meridian session log p2"
    )


def test_spawn_action_terminal_failed_includes_error_report_and_transcript() -> None:
    output = SpawnActionOutput(
        command="spawn.create",
        status="failed",
        spawn_id="p3",
        error="execution_crash",
        report="partial",
        exit_code=1,
    )

    text = output.format_text()
    assert (
        text
        == "p3 failed\nError: execution_crash\n\npartial\n\nTranscript: meridian session log p3"
    )
    assert text.index("Error: execution_crash") < text.index("partial")


def test_spawn_action_terminal_cancelled_without_report_keeps_transcript() -> None:
    output = SpawnActionOutput(command="spawn.create", status="cancelled", spawn_id="p4")

    text = output.format_text()
    assert text == "p4 cancelled\n\nTranscript: meridian session log p4"
    assert "(no report)" not in text


def test_spawn_action_non_create_keeps_default_text() -> None:
    output = SpawnActionOutput(command="spawn.cancel", status="succeeded", spawn_id="p123")

    assert output.format_text() == "Spawn succeeded.\nSpawn id: p123"


def test_spawn_detail_wait_failed_orphan_compact_text_exact() -> None:
    detail = _make_spawn_detail(
        status="failed",
        duration_secs=2.5,
        exit_code=1,
        failure_reason="orphan_finalization",
        report_body="partial",
    )

    assert detail.format_wait_text() == (
        "p1 failed (exit 1) (2.5s)\n"
        "Failure: orphan_finalization (harness likely completed; "
        "report.md may still contain useful content)\n"
        "\n"
        "partial\n"
        "\n"
        "Transcript: meridian session log p1"
    )


def test_spawn_detail_wait_no_report_still_has_transcript() -> None:
    detail = _make_spawn_detail(report_body=None)

    text = detail.format_wait_text()
    assert text == "p1 succeeded\n\nTranscript: meridian session log p1"
    assert "(no report)" not in text


def test_spawn_wait_multi_json_single_and_multi_projection() -> None:
    single = SpawnWaitMultiOutput(
        spawns=(_make_spawn_detail(spawn_id="p1", report_body="single report"),),
        total_runs=1,
        succeeded_runs=1,
        failed_runs=0,
        cancelled_runs=0,
        any_failed=False,
        spawn_id="p1",
        status="succeeded",
        exit_code=0,
    )
    single_wire = single.to_cli_wire()
    assert single_wire["report_body"] == "single report"
    assert single_wire["transcript_command"] == "meridian session log p1"
    assert single_wire["spawns"][0]["report_body"] == "single report"
    assert single_wire["spawns"][0]["transcript_command"] == "meridian session log p1"

    multi = SpawnWaitMultiOutput(
        spawns=(
            _make_spawn_detail(spawn_id="p1", report_body="first report"),
            _make_spawn_detail(spawn_id="p2", report_body="second report"),
        ),
        total_runs=2,
        succeeded_runs=2,
        failed_runs=0,
        cancelled_runs=0,
        any_failed=False,
    )
    multi_wire = multi.to_cli_wire()
    assert "report_body" not in multi_wire
    assert "transcript_command" not in multi_wire
    assert multi_wire["spawns"][0]["transcript_command"] == "meridian session log p1"
    assert multi_wire["spawns"][1]["transcript_command"] == "meridian session log p2"
    assert multi_wire["spawns"][0]["report_body"] == "first report"
    assert multi_wire["spawns"][1]["report_body"] == "second report"


def test_spawn_action_wire_background_does_not_add_transcript_command() -> None:
    output = SpawnActionOutput(
        command="spawn.create",
        status="running",
        spawn_id="pbg",
        background=True,
    )

    wire = output.to_wire()
    assert wire["status"] == "running"
    assert wire["spawn_id"] == "pbg"
    assert wire["terminal"] is False
    assert wire["wait_required"] is True
    assert wire["wait_command"] == "meridian spawn wait"
    assert wire["note"].startswith("Background spawn submitted.")
    assert "transcript_command" not in wire

    agent_wire = output.to_agent_wire()
    assert agent_wire["status"] == "running"
    assert agent_wire["spawn_id"] == "pbg"
    assert agent_wire["terminal"] is False
    assert agent_wire["wait_required"] is True
    assert agent_wire["wait_command"] == "meridian spawn wait"
    assert "transcript_command" not in agent_wire


def test_spawn_action_dry_run_exposes_matched_policy_rule() -> None:
    output = SpawnActionOutput(
        command="spawn.create",
        status="dry-run",
        model="gpt-5.5",
        harness_id="codex",
        matched_policy_rule="settings:2",
    )

    wire = output.to_wire()
    text = output.format_text()

    assert wire["matched_policy_rule"] == "settings:2"
    assert "Matched policy rule: settings:2" in text
