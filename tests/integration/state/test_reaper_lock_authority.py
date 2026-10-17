"""Reaper writes are authorized by the runner-lifetime launch lock."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state import spawn_store
from meridian.lib.state.reaper import reconcile_active_spawn


def _active_spawn(runtime_root: Path, *, spawn_id: str = "p1") -> str:
    created = spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="test",
        agent="tester",
        harness="claude",
        kind="child",
        prompt="hello",
        runner_pid=999_999,
        started_at="2000-01-01T00:00:00Z",
        status="running",
    )
    (runtime_root / "spawns" / str(created) / "launch.lock").touch()
    return str(created)


def test_runner_exit_does_not_turn_a_crashed_launch_into_success(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = _active_spawn(runtime_root)
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status="succeeded",
        exit_code=0,
        exited_at="2000-01-01T00:00:01Z",
    )
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None

    reconciled = reconcile_active_spawn(tmp_path, runtime_root, row)

    assert reconciled.status == "failed"
    assert reconciled.terminal is not None
    assert reconciled.terminal.exit_code == 1
    assert reconciled.terminal.error == "crashed"


def test_dead_runner_with_cancel_intent_is_cancelled(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = _active_spawn(runtime_root)
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled by user",
        requested_at="2000-01-01T00:00:01Z",
    )
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None

    reconciled = reconcile_active_spawn(tmp_path, runtime_root, row)

    assert reconciled.status == "cancelled"
    assert reconciled.terminal is not None
    assert reconciled.terminal.exit_code == 130
    assert reconciled.terminal.error == "cancelled by user"
    assert reconciled.terminal.origin == "reconciler"
