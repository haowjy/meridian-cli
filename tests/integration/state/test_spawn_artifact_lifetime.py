"""Spawn-owned artifacts cannot outlive their published row."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.clock import RealClock
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessConnection, HarnessRequest
from meridian.lib.harness.connections.managed_backend import register_spawn_owned_process
from meridian.lib.harness.control_action import ControlActionCoordinator, ControlActionType
from meridian.lib.harness.permission_broker import PermissionBroker
from meridian.lib.launch.process.runner import _write_native_primary_metadata
from meridian.lib.launch.streaming_runner import _append_runner_lifecycle_event
from meridian.lib.state.failure_sentinel import write_failure_sentinel
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.reaper import (
    _collect_artifact_snapshot,
    _log_orphan_primary_diagnostics,
    _record_orphan_finalize_evidence,
)
from meridian.lib.state.spawn_aggregate import delete_published_spawn
from meridian.lib.state.spawn_scope import write_spawn_scope_task_dir
from meridian.lib.state.spawn_signals import write_spawn_signal
from meridian.lib.state.spawn_store import finalize_spawn, get_spawn, start_spawn


def _start_test_spawn(
    runtime_root: Path,
    spawn_id: str,
    *,
    status: SpawnStatus,
) -> None:
    start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.6",
        agent="coder",
        harness="codex",
        prompt="test",
        spawn_id=spawn_id,
        status=status,
    )


def test_late_failure_sentinel_does_not_recreate_deleted_spawn(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    spawn_dir = runtime_root / "spawns" / spawn_id
    _start_test_spawn(runtime_root, spawn_id, status="running")
    finalize_spawn(runtime_root, spawn_id, "failed", 1, origin="runner")

    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda record: record is not None,
    )

    write_failure_sentinel(runtime_root, spawn_id, {"reason": "late"})

    assert not spawn_dir.exists()


def test_failure_sentinel_refuses_non_failed_spawn(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    sentinel_path = runtime_root / "spawns" / spawn_id / "failure.json"
    _start_test_spawn(runtime_root, spawn_id, status="running")
    finalize_spawn(runtime_root, spawn_id, "succeeded", 0, origin="runner")

    write_failure_sentinel(runtime_root, spawn_id, {"reason": "stale"})

    assert not sentinel_path.exists()


def test_late_spawn_signal_does_not_recreate_deleted_spawn(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    spawn_dir = runtime_root / "spawns" / spawn_id
    _start_test_spawn(runtime_root, spawn_id, status="running")
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda record: record is not None,
    )

    assert write_spawn_signal(runtime_root, spawn_id, "done") is False

    assert not spawn_dir.exists()


def test_late_spawn_scope_write_does_not_recreate_deleted_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    project_root = tmp_path / "repo"
    (project_root / ".meridian").mkdir(parents=True)
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    spawn_id = "p1"
    spawn_dir = runtime_root / "spawns" / spawn_id
    _start_test_spawn(runtime_root, spawn_id, status="running")
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda record: record is not None,
    )

    assert write_spawn_scope_task_dir(project_root, spawn_id, tmp_path) is False

    assert not spawn_dir.exists()


@pytest.mark.asyncio
async def test_control_action_ack_does_not_recreate_deleted_spawn(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    spawn_dir = runtime_root / "spawns" / spawn_id
    _start_test_spawn(runtime_root, spawn_id, status="running")
    coordinator = ControlActionCoordinator(spawn_id=SpawnId(spawn_id), spawn_dir=spawn_dir)
    send_started = asyncio.Event()
    finish_send = asyncio.Event()

    async def _send() -> object:
        send_started.set()
        await finish_send.wait()
        return None

    action = asyncio.create_task(
        coordinator.run_action(
            action=ControlActionType.INJECT,
            payload={"text": "late"},
            source="test",
            send=_send,
        )
    )
    await send_started.wait()
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda record: record is not None,
    )
    finish_send.set()

    result = await action

    assert result.success is True
    assert not spawn_dir.exists()


@pytest.mark.asyncio
async def test_permission_cursor_does_not_recreate_deleted_spawn(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    spawn_dir = runtime_root / "spawns" / spawn_id
    _start_test_spawn(runtime_root, spawn_id, status="running")
    dispatch_started = asyncio.Event()
    finish_dispatch = asyncio.Event()

    async def _event_sink(_event: object) -> None:
        dispatch_started.set()
        await finish_dispatch.wait()

    broker = PermissionBroker(spawn_dir=spawn_dir, event_sink=_event_sink)
    request = HarnessRequest(
        request_id="approval-1",
        request_type="approval",
        method="item/commandExecution/requestApproval",
        payload={"command": "echo hi"},
    )
    dispatch = asyncio.create_task(
        broker.handle_request(cast("HarnessConnection[Any]", object()), request)
    )
    await dispatch_started.wait()
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda record: record is not None,
    )
    finish_dispatch.set()

    await dispatch

    assert not spawn_dir.exists()


@pytest.mark.asyncio
async def test_late_permission_transition_does_not_recreate_deleted_spawn(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    spawn_dir = runtime_root / "spawns" / spawn_id
    _start_test_spawn(runtime_root, spawn_id, status="running")
    broker = PermissionBroker(spawn_dir=spawn_dir)
    await broker.handle_request(
        cast("HarnessConnection[Any]", object()),
        HarnessRequest(
            request_id="approval-1",
            request_type="approval",
            method="item/commandExecution/requestApproval",
            payload={"command": "echo hi"},
        ),
    )
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda record: record is not None,
    )

    await broker.on_request_resolved(
        "approval-1",
        resolution={"decision": "accept"},
    )

    assert not spawn_dir.exists()


def test_late_runner_lifecycle_write_does_not_recreate_deleted_spawn(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = SpawnId("p1")
    spawn_dir = runtime_root / "spawns" / str(spawn_id)
    lifecycle_path = spawn_dir / "runner-lifecycle.jsonl"
    _start_test_spawn(runtime_root, str(spawn_id), status="running")
    _append_runner_lifecycle_event(
        runtime_root,
        spawn_id,
        lifecycle_path,
        clock=RealClock(),
        event="runner_started",
        phase="setup",
    )
    assert lifecycle_path.is_file()
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda row: row is not None,
    )

    _append_runner_lifecycle_event(
        runtime_root,
        spawn_id,
        lifecycle_path,
        clock=RealClock(),
        event="runner_completed",
        phase="completed",
    )

    assert not spawn_dir.exists()


def test_late_reaper_evidence_write_does_not_recreate_deleted_spawn(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    spawn_dir = runtime_root / "spawns" / spawn_id
    evidence_path = spawn_dir / "finalize-evidence.json"
    _start_test_spawn(runtime_root, spawn_id, status="running")
    record = get_spawn(runtime_root, spawn_id)
    assert record is not None
    now = time.time()
    snapshot = _collect_artifact_snapshot(runtime_root, record, now)
    _record_orphan_finalize_evidence(runtime_root, record, snapshot, now)
    assert evidence_path.is_file()
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda row: row is not None,
    )

    _record_orphan_finalize_evidence(runtime_root, record, snapshot, now)

    assert not spawn_dir.exists()


def test_reaper_evidence_refuses_terminal_spawn(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    evidence_path = runtime_root / "spawns" / spawn_id / "finalize-evidence.json"
    _start_test_spawn(runtime_root, spawn_id, status="running")
    finalize_spawn(runtime_root, spawn_id, "succeeded", 0, origin="runner")
    record = get_spawn(runtime_root, spawn_id)
    assert record is not None
    now = time.time()
    snapshot = _collect_artifact_snapshot(runtime_root, record, now)

    _record_orphan_finalize_evidence(runtime_root, record, snapshot, now)

    assert not evidence_path.exists()


def test_reaper_evidence_uses_scope_liveness_for_shim_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
    from meridian.lib.state.process_scope_projection import record_scope

    runtime_root = tmp_path / "runtime"
    spawn_id = SpawnId("p1")
    evidence_path = runtime_root / "spawns" / str(spawn_id) / "finalize-evidence.json"
    _start_test_spawn(runtime_root, str(spawn_id), status="running")
    scope = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id=str(spawn_id),
        role="harness_backend",
        containment="posix_pgid",
        root_pid=41001,
        root_created_at_epoch=100.0,
        pgid=41001,
        job_name=None,
        degraded_reason=None,
    )
    record_scope(runtime_root, spawn_id, scope)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda _pid, created_after_epoch=None: False,
    )
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_pgid_reachable",
        lambda _pgid: True,
    )
    record = get_spawn(runtime_root, str(spawn_id))
    assert record is not None
    now = time.time()
    snapshot = _collect_artifact_snapshot(runtime_root, record, now)

    _record_orphan_finalize_evidence(runtime_root, record, snapshot, now)

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    backend = evidence["child_processes"][0]
    assert backend == {
        "scope_id": "backend",
        "role": "harness_backend",
        "pid": 41001,
        "root_alive": False,
        "pgid_reachable": True,
        "likely_serving": True,
        "alive": True,
    }
    assert evidence["worker_or_backend_alive"] is True


def test_orphan_primary_diagnostics_use_scope_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
    from meridian.lib.state.managed_primary import ManagedPrimarySnapshot
    from meridian.lib.state.primary_meta import PrimaryMetadata
    from meridian.lib.state.process_scope_projection import record_scope

    runtime_root = tmp_path / "runtime"
    spawn_id = SpawnId("p1")
    _start_test_spawn(runtime_root, str(spawn_id), status="running")
    record_scope(
        runtime_root,
        spawn_id,
        ProcessScopeSnapshot(
            scope_id="backend",
            owner_policy="spawn_owned",
            owner_id=str(spawn_id),
            role="harness_backend",
            containment="posix_pgid",
            root_pid=41001,
            root_created_at_epoch=100.0,
            pgid=41001,
            job_name=None,
            degraded_reason=None,
        ),
    )
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda _pid, created_after_epoch=None: False,
    )
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_pgid_reachable",
        lambda _pgid: True,
    )
    warnings: list[dict[str, object]] = []

    class _Logger:
        def warning(self, _message: str, **fields: object) -> None:
            warnings.append(fields)

    monkeypatch.setattr("meridian.lib.state.reaper.logger", _Logger())
    record = get_spawn(runtime_root, str(spawn_id))
    assert record is not None
    now = time.time()

    _log_orphan_primary_diagnostics(
        runtime_root,
        record,
        _collect_artifact_snapshot(runtime_root, record, now),
        ManagedPrimarySnapshot(
            metadata=PrimaryMetadata(
                managed_backend=True,
                launcher_pid=42001,
                backend_pid=41001,
                activity="idle",
            ),
            launcher_pid_alive=False,
            started_epoch=100.0,
        ),
    )

    assert warnings[0]["backend_alive"] is True
    assert warnings[0]["backend_root_alive"] is False
    assert warnings[0]["backend_pgid_reachable"] is True


def test_late_native_primary_metadata_does_not_recreate_deleted_spawn(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = SpawnId("p1")
    spawn_dir = runtime_root / "spawns" / str(spawn_id)
    metadata_path = spawn_dir / "primary_meta.json"
    _start_test_spawn(runtime_root, str(spawn_id), status="running")

    def _write() -> None:
        _write_native_primary_metadata(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            spawn_dir=spawn_dir,
            command=("pi",),
            launch_cwd=tmp_path,
            launcher_pid=os.getpid(),
            tui_pid=None,
            activity="finalizing",
            started_at_epoch=time.time(),
            ended_at_epoch=time.time(),
            exit_code=0,
            harness_session_id="session-1",
        )

    _write()
    assert metadata_path.is_file()
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda row: row is not None,
    )

    _write()

    assert not spawn_dir.exists()


@pytest.mark.asyncio
async def test_process_adoption_does_not_recreate_deleted_spawn(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    project_root = tmp_path / "repo"
    spawn_id = SpawnId("p1")
    spawn_dir = runtime_root / "spawns" / str(spawn_id)
    _start_test_spawn(runtime_root, str(spawn_id), status="running")
    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda row: row is not None,
    )
    import asyncio

    from meridian.lib.platform.launch_gate import LaunchGate

    gate = LaunchGate()
    launch = gate.spawn_kwargs(("/bin/sleep", "60"), os.environ)
    process = await asyncio.create_subprocess_exec(
        *launch["args"],
        env=launch["env"],
        pass_fds=launch["pass_fds"],
        start_new_session=launch["start_new_session"],
    )
    try:
        with pytest.raises(ValueError, match="spawn does not exist"):
            await register_spawn_owned_process(
                spawn_id=spawn_id,
                control_root=project_root,
                runtime_root=runtime_root,
                process=process,
                identity=gate.adopt(process.pid),
                gate=gate,
                scope_id="backend",
                role="harness_backend",
            )
        # OS reaping can precede asyncio publishing its returncode callback.
        await asyncio.wait_for(process.wait(), timeout=5)
        assert process.returncode is not None
        assert not spawn_dir.exists()
    finally:
        gate.close()
        if process.returncode is None:
            process.kill()
        await process.wait()
