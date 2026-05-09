"""Spawn output model formatting regressions."""

from meridian.lib.ops.spawn.models import SpawnDetailOutput


def _spawn_detail(**overrides: object) -> SpawnDetailOutput:
    values: dict[str, object] = {
        "spawn_id": "p42",
        "status": "running",
        "model": "gpt-5.4",
        "harness": "codex",
        "started_at": "2026-04-21T00:00:00Z",
        "finished_at": None,
        "duration_secs": None,
        "exit_code": None,
        "failure_reason": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "report_path": None,
        "report_summary": None,
        "report_body": None,
        "log_path": "/tmp/spawns/p42/stderr.log",
    }
    values.update(overrides)
    return SpawnDetailOutput.model_validate(values)


def test_spawn_detail_active_output_points_to_session_log_not_stderr_tail() -> None:
    text = _spawn_detail().format_text()

    assert "Progress: meridian session log p42" in text
    assert "tail -f" not in text
    assert "stderr.log" not in text


def test_spawn_detail_with_harness_session_points_to_session_log() -> None:
    text = _spawn_detail(harness_session_id="thread-123").format_text()

    assert "Transcript: meridian session log p42" in text


def test_spawn_detail_active_status_hides_attempt_exit_fields() -> None:
    text = _spawn_detail(
        status="running",
        exited_at="2026-04-23T12:00:00Z",
        process_exit_code=1,
    ).format_text()

    assert "Exited at:" not in text
    assert "Process exit code:" not in text


def test_spawn_detail_terminal_status_shows_attempt_exit_fields() -> None:
    text = _spawn_detail(
        status="failed",
        exited_at="2026-04-23T12:00:00Z",
        process_exit_code=1,
    ).format_text()

    assert "Exited at: 2026-04-23T12:00:00Z" in text
    assert "Process exit code: 1" in text


def test_spawn_detail_includes_goal_in_text_and_wire() -> None:
    detail = _spawn_detail(status="succeeded", goal="ship phase 3")

    assert "Goal: ship phase 3" in detail.format_text()
    assert detail.to_cli_wire()["goal"] == "ship phase 3"
