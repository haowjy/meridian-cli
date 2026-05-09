from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import meridian.lib.ops.spawn.execute as spawn_execute
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.ops.runtime import resolve_runtime_root
from meridian.lib.ops.spawn.execute import (
    BackgroundWorkerLaunchRequest,
    execute_spawn_background,
)
from meridian.lib.ops.spawn.failure_policy import finalize_launch_failure_sync
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_boundary import (
    EVENT_PARENT_LAUNCH_ATTEMPT,
    EVENT_PARENT_LAUNCH_FAILED,
    EVENT_PARENT_LAUNCH_SPAWNED,
    read_launch_boundary_events,
)


class _FakeProcess:
    pid = 4242


def test_execute_spawn_background_persists_runtime_override_snapshot_and_launch_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_APPROVAL", "confirm")
    monkeypatch.setattr(
        spawn_execute.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FakeProcess(),
    )

    result = execute_spawn_background(
        payload=SpawnCreateInput(
            prompt="run it",
            model="gpt-5.4",
            harness="codex",
            background=True,
            project_root=tmp_path.as_posix(),
        ),
        request=SpawnRequest(
            prompt="run it",
            model="gpt-5.4",
            harness="codex",
            goal="ship phase 3",
        ),
        runtime=SimpleNamespace(project_root=tmp_path, sink=None),
    )

    runtime_root = resolve_runtime_root(tmp_path)
    log_dir = runtime_root / "spawns" / str(result.spawn_id)
    request_path = log_dir / "bg-worker-request.json"
    params_path = log_dir / "params.json"
    persisted = BackgroundWorkerLaunchRequest.model_validate_json(
        request_path.read_text(encoding="utf-8")
    )
    params = json.loads(params_path.read_text(encoding="utf-8"))
    events = read_launch_boundary_events(runtime_root, str(result.spawn_id))
    record = spawn_store.get_spawn(runtime_root, str(result.spawn_id))

    assert result.status == "running"
    assert persisted.runtime.runtime_override_snapshot == {"approval": "confirm"}
    assert [event.event for event in events] == [
        EVENT_PARENT_LAUNCH_ATTEMPT,
        EVENT_PARENT_LAUNCH_SPAWNED,
    ]
    assert events[-1].launcher_pid == 4242
    assert record is not None
    assert record.status == "running"
    assert record.goal == "ship phase 3"
    assert record.runner_pid == 4242
    assert record.launch_mode == "background"
    assert params["goal"] == "ship phase 3"


def test_finalize_launch_failure_sync_keeps_fixed_terminal_tuple(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    service = create_lifecycle_service(tmp_path, runtime_root)
    spawn_id = SpawnId(
        service.start(
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="run it",
            spawn_id="p1",
            status="queued",
        )
    )

    outcome = finalize_launch_failure_sync(runtime_root, tmp_path, spawn_id, "boom")

    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert outcome.wrote is True
    assert record is not None
    assert record.status == "failed"
    assert record.exit_code == 1
    assert record.terminal_origin == "launch_failure"
    assert record.error == "boom"


def test_execute_spawn_background_marks_launch_failure_when_worker_request_persist_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_persist(_log_dir: Path, _payload: BackgroundWorkerLaunchRequest) -> None:
        raise OSError("persist boom")

    monkeypatch.setattr(spawn_execute, "_persist_bg_worker_request", fail_persist)

    result = execute_spawn_background(
        payload=SpawnCreateInput(
            prompt="run it",
            model="gpt-5.4",
            harness="codex",
            background=True,
            project_root=tmp_path.as_posix(),
        ),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
        runtime=SimpleNamespace(project_root=tmp_path, sink=None),
    )

    runtime_root = resolve_runtime_root(tmp_path)
    record = spawn_store.get_spawn(runtime_root, str(result.spawn_id))
    events = read_launch_boundary_events(runtime_root, str(result.spawn_id))
    log_dir = runtime_root / "spawns" / str(result.spawn_id)

    assert result.status == "failed"
    assert result.error == "background_launch_failed"
    assert record is not None
    assert record.status == "failed"
    assert record.exit_code == 1
    assert record.terminal_origin == "launch_failure"
    assert record.error == "persist boom"
    assert [event.event for event in events] == [
        EVENT_PARENT_LAUNCH_ATTEMPT,
        EVENT_PARENT_LAUNCH_FAILED,
    ]
    assert events[-1].stage == "persist_worker_request"
    assert events[-1].error == "persist boom"
    assert not (log_dir / "bg-worker-request.json").exists()


def test_execute_spawn_background_marks_launch_failure_when_popen_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("popen boom")

    monkeypatch.setattr(spawn_execute.subprocess, "Popen", fail_popen)

    result = execute_spawn_background(
        payload=SpawnCreateInput(
            prompt="run it",
            model="gpt-5.4",
            harness="codex",
            background=True,
            project_root=tmp_path.as_posix(),
        ),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
        runtime=SimpleNamespace(project_root=tmp_path, sink=None),
    )

    runtime_root = resolve_runtime_root(tmp_path)
    record = spawn_store.get_spawn(runtime_root, str(result.spawn_id))
    events = read_launch_boundary_events(runtime_root, str(result.spawn_id))
    log_dir = runtime_root / "spawns" / str(result.spawn_id)

    assert result.status == "failed"
    assert result.error == "background_launch_failed"
    assert record is not None
    assert record.status == "failed"
    assert record.exit_code == 1
    assert record.terminal_origin == "launch_failure"
    assert record.error == "popen boom"
    assert [event.event for event in events] == [
        EVENT_PARENT_LAUNCH_ATTEMPT,
        EVENT_PARENT_LAUNCH_FAILED,
    ]
    assert events[-1].stage == "popen"
    assert events[-1].error == "popen boom"
    assert not (log_dir / "bg-worker-request.json").exists()
