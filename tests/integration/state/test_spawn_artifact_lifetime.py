"""Spawn-owned artifacts cannot outlive their published row."""

import asyncio
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessConnection, HarnessRequest
from meridian.lib.harness.control_action import ControlActionCoordinator, ControlActionType
from meridian.lib.harness.permission_broker import PermissionBroker
from meridian.lib.launch.streaming.heartbeat import FileHeartbeat
from meridian.lib.state.failure_sentinel import write_failure_sentinel
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn_aggregate import delete_published_spawn
from meridian.lib.state.spawn_scope import write_spawn_scope_task_dir
from meridian.lib.state.spawn_signals import write_spawn_signal
from meridian.lib.state.spawn_store import start_spawn
from meridian.lib.streaming.heartbeat import heartbeat_loop


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
    _start_test_spawn(runtime_root, spawn_id, status="failed")

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
    _start_test_spawn(runtime_root, spawn_id, status="succeeded")

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


def test_file_heartbeat_does_not_recreate_deleted_parent(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p1"
    heartbeat_path = spawn_dir / "heartbeat"
    spawn_dir.mkdir(parents=True)
    heartbeat = FileHeartbeat(heartbeat_path)
    heartbeat.touch()
    assert heartbeat_path.is_file()

    shutil.rmtree(spawn_dir)
    heartbeat.touch()

    assert not spawn_dir.exists()


@pytest.mark.asyncio
async def test_heartbeat_loop_stops_when_spawn_parent_is_missing(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    await asyncio.wait_for(
        heartbeat_loop(runtime_root, SpawnId("p1"), interval=0.001),
        timeout=0.1,
    )

    assert not (runtime_root / "spawns" / "p1").exists()


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
