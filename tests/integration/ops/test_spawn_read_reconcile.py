import os
from pathlib import Path

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.ops.spawn.models import (
    ModelStats,
    SpawnListInput,
    SpawnShowInput,
    SpawnStatsOutput,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root


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
