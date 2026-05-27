import os
import time
from datetime import UTC, datetime
from pathlib import Path

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.spawn import query as spawn_query
from meridian.lib.ops.spawn.models import (
    SpawnShowInput,
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
    assert "Status: finalizing" in rendered
    verbose_rendered = output.format_text(FormatContext(verbosity=1))
    assert "Status: finalizing (cleanup in progress)" in verbose_rendered
    assert "orphan_finalization" in verbose_rendered
    assert "report.md may still contain useful content" in verbose_rendered
    assert "awaiting finalization" not in verbose_rendered


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


def test_read_spawn_row_nested_recent_disk_activity_keeps_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    for spawn_id, activity_file in [
        ("p-heartbeat", "heartbeat"),
        ("p-history", "history.jsonl"),
    ]:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id=spawn_id,
            chat_id="c1",
            model="gpt-5.3-codex",
            agent="coder",
            harness="codex",
            prompt="hello",
            runner_pid=999_999_999,
            started_at=_OLD_TIMESTAMP,
        )
        path = runtime_root / "spawns" / spawn_id / activity_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        now = time.time()
        os.utime(path, (now, now))

        result = spawn_query.read_spawn_row(project_root, spawn_id, runtime_root=runtime_root)

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
        spawn_id="p-startup-grace",
        chat_id="c1",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="hello",
        runner_pid=999_999_999,
        started_at=recent_started_at,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    result = spawn_query.read_spawn_row(
        project_root,
        "p-startup-grace",
        runtime_root=runtime_root,
    )

    assert result is not None
    assert result.status == "running"
    assert result.exit_code is None
    assert result.error is None


def test_read_spawn_row_nested_runner_exit_after_grace_returns_synthetic_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_running_spawn(runtime_root, "p-runner-exit")
    spawn_store.record_runner_exit(
        runtime_root,
        "p-runner-exit",
        status="failed",
        exit_code=17,
        error="runner_failed",
        exited_at=_OLD_TIMESTAMP,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    result = spawn_query.read_spawn_row(project_root, "p-runner-exit", runtime_root=runtime_root)

    assert result is not None
    assert result.status == "failed"
    assert result.exit_code == 17
    assert result.error == "runner_failed"

    persisted = spawn_store.get_spawn(runtime_root, "p-runner-exit")
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.runner_exit_status == "failed"
    assert persisted.exit_code is None
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
        spawn_id="p-wait-stale",
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
            spawn_ids=("p-wait-stale",),
            timeout=0.01,
            timeout_explicit=True,
            poll_interval_secs=0.01,
            project_root=project_root.as_posix(),
        )
    )

    assert output.any_failed is True
    assert len(output.spawns) == 1
    assert output.spawns[0].spawn_id == "p-wait-stale"
    assert output.spawns[0].status == "failed"
    assert output.spawns[0].exit_code == 1
    assert output.spawns[0].failure_reason == "stale_nested_read"

    persisted = spawn_store.get_spawn(runtime_root, "p-wait-stale")
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.exit_code is None
    assert persisted.error is None
