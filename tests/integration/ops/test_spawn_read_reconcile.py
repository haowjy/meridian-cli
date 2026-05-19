import os
import time
from datetime import UTC, datetime
from pathlib import Path

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.launch.constants import PI_LIFECYCLE_EVENTS_FILENAME
from meridian.lib.ops.spawn import query as spawn_query
from meridian.lib.ops.spawn.models import (
    ModelStats,
    SpawnListInput,
    SpawnShowInput,
    SpawnStatsOutput,
    SpawnWaitInput,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root

_OLD_TIMESTAMP = "2000-01-01T00:00:00Z"


def _state_root(project_root: Path) -> Path:
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _seed_running_spawn(runtime_root: Path, spawn_id: str) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="hello",
        runner_pid=os.getpid(),
    )


def test_spawn_show_sync_renders_finalizing_status_and_orphan_finalization_hint(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_running_spawn(runtime_root, "p1")
    spawn_store.record_spawn_exited(
        runtime_root,
        "p1",
        exit_code=143,
        exited_at="2026-04-12T14:00:00Z",
    )
    assert spawn_store.mark_finalizing(runtime_root, "p1") is True
    spawn_store.update_spawn(runtime_root, "p1", error="orphan_finalization")
    heartbeat = runtime_root / "spawns" / "p1" / "heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch(exist_ok=True)

    output = spawn_api.spawn_show_sync(
        SpawnShowInput(
            spawn_id="p1",
            include_report_body=False,
            project_root=project_root.as_posix(),
        )
    )

    assert output.spawn_id == "p1"
    assert output.status == "finalizing"
    assert output.last_attempt_exited_at == "2026-04-12T14:00:00Z"
    assert output.last_attempt_exit_code == 143
    rendered = output.format_text()
    assert "Status: finalizing (cleanup in progress)" in rendered
    assert "orphan_finalization" in rendered
    assert "report.md may still contain useful content" in rendered
    assert "awaiting finalization" not in rendered


def test_spawn_list_sync_no_longer_renders_running_asterisk_suffix(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_running_spawn(runtime_root, "p2")
    spawn_store.record_spawn_exited(
        runtime_root,
        "p2",
        exit_code=0,
        exited_at="2026-04-12T14:00:00Z",
    )

    output = spawn_api.spawn_list_sync(SpawnListInput(project_root=project_root.as_posix()))

    assert len(output.spawns) == 1
    assert output.spawns[0].status == "running"
    assert output.spawns[0].status_display is None
    rendered = output.format_text()
    assert "running*" not in rendered
    assert "running" in rendered


def test_spawn_stats_output_tracks_finalizing_as_active_bucket() -> None:
    stats = SpawnStatsOutput(
        total_runs=3,
        succeeded=1,
        failed=1,
        cancelled=0,
        running=1,
        finalizing=1,
        total_duration_secs=7.5,
        total_cost_usd=0.12,
        models={
            "gpt-5.3-codex": ModelStats(
                total=3,
                succeeded=1,
                failed=1,
                cancelled=0,
                running=1,
                finalizing=1,
                cost_usd=0.12,
            )
        },
    )

    assert stats.model_dump()["finalizing"] == 1
    assert stats.models["gpt-5.3-codex"].finalizing == 1
    rendered = stats.format_text()
    assert "running: 1" in rendered
    assert "finalizing: 1" in rendered


def test_read_spawn_row_nested_stale_dead_runner_returns_synthetic_failed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p3",
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="hello",
        runner_pid=999_999_999,
        started_at=_OLD_TIMESTAMP,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    result = spawn_query.read_spawn_row(project_root, "p3", runtime_root=runtime_root)

    assert result is not None
    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.error == "stale_nested_read"

    persisted = spawn_store.get_spawn(runtime_root, "p3")
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.exit_code is None
    assert persisted.error is None


def test_read_spawn_row_nested_runner_exit_after_grace_returns_synthetic_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_running_spawn(runtime_root, "p4")
    spawn_store.record_runner_exit(
        runtime_root,
        "p4",
        status="failed",
        exit_code=17,
        error="runner_failed",
        exited_at=_OLD_TIMESTAMP,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    result = spawn_query.read_spawn_row(project_root, "p4", runtime_root=runtime_root)

    assert result is not None
    assert result.status == "failed"
    assert result.exit_code == 17
    assert result.error == "runner_failed"

    persisted = spawn_store.get_spawn(runtime_root, "p4")
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.runner_exit_status == "failed"
    assert persisted.exit_code is None
    assert persisted.error is None


def test_read_spawn_row_nested_recent_activity_keeps_running_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p5",
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="hello",
        runner_pid=999_999_999,
        started_at=_OLD_TIMESTAMP,
    )
    heartbeat = runtime_root / "spawns" / "p5" / "heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch(exist_ok=True)
    now = time.time()
    os.utime(heartbeat, (now, now))
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    result = spawn_query.read_spawn_row(project_root, "p5", runtime_root=runtime_root)

    assert result is not None
    assert result.status == "running"
    assert result.error is None


def test_read_spawn_row_nested_recent_pi_lifecycle_activity_keeps_running_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p5b",
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="hello",
        runner_pid=999_999_999,
        started_at=_OLD_TIMESTAMP,
    )
    lifecycle_events = (
        runtime_root / "spawns" / "p5b" / PI_LIFECYCLE_EVENTS_FILENAME
    )
    lifecycle_events.parent.mkdir(parents=True, exist_ok=True)
    lifecycle_events.write_text("", encoding="utf-8")
    now = time.time()
    os.utime(lifecycle_events, (now, now))
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    result = spawn_query.read_spawn_row(project_root, "p5b", runtime_root=runtime_root)

    assert result is not None
    assert result.status == "running"
    assert result.error is None


def test_read_spawn_row_nested_stale_missing_runner_pid_returns_synthetic_failed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p6",
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="hello",
        runner_pid=None,
        started_at=_OLD_TIMESTAMP,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    result = spawn_query.read_spawn_row(project_root, "p6", runtime_root=runtime_root)

    assert result is not None
    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.error == "stale_nested_read_no_pid"

    persisted = spawn_store.get_spawn(runtime_root, "p6")
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.error is None


def test_spawn_wait_sync_nested_stale_active_returns_synthetic_terminal_without_persisting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p7",
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="hello",
        runner_pid=999_999_999,
        started_at=_OLD_TIMESTAMP,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    output = spawn_api.spawn_wait_sync(
        SpawnWaitInput(
            spawn_ids=("p7",),
            timeout=0.01,
            timeout_explicit=True,
            poll_interval_secs=0.01,
            project_root=project_root.as_posix(),
        )
    )

    assert output.total_runs == 1
    assert output.any_failed is True
    assert len(output.spawns) == 1
    assert output.spawns[0].spawn_id == "p7"
    assert output.spawns[0].status == "failed"
    assert output.spawns[0].exit_code == 1
    assert output.spawns[0].failure_reason == "stale_nested_read"

    persisted = spawn_store.get_spawn(runtime_root, "p7")
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.exit_code is None
    assert persisted.error is None


def test_read_spawn_row_nested_startup_grace_keeps_running_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    recent_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p8",
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="hello",
        runner_pid=999_999_999,
        started_at=recent_started_at,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    result = spawn_query.read_spawn_row(project_root, "p8", runtime_root=runtime_root)

    assert result is not None
    assert result.status == "running"
    assert result.exit_code is None
    assert result.error is None


def test_read_spawn_row_nested_post_runner_exit_grace_keeps_running_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_running_spawn(runtime_root, "p9")
    recent_exited_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    spawn_store.record_runner_exit(
        runtime_root,
        "p9",
        status="failed",
        exit_code=17,
        error="runner_failed",
        exited_at=recent_exited_at,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    result = spawn_query.read_spawn_row(project_root, "p9", runtime_root=runtime_root)

    assert result is not None
    assert result.status == "running"
    assert result.runner_exit_status == "failed"
    assert result.exit_code is None
    assert result.error is None
