"""Launch-boundary storage and lock placement regressions."""

import threading
from pathlib import Path

import pytest

from meridian.lib.state import launch_boundary as launch_boundary_module
from meridian.lib.state.launch_boundary import (
    launch_boundary_lock_path,
    record_launch_boundary_event,
)
from meridian.lib.state.spawn_aggregate import delete_published_spawn
from meridian.lib.state.spawn_store import start_spawn


def _start_test_spawn(runtime_root: Path, spawn_id: str) -> None:
    start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.6",
        agent="coder",
        harness="codex",
        prompt="test",
        spawn_id=spawn_id,
        status="succeeded",
    )


def test_lock_lives_outside_spawn_dir(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    _start_test_spawn(runtime_root, spawn_id)

    record_launch_boundary_event(
        runtime_root,
        spawn_id,
        event="worker_boot",
        worker_pid=123,
    )

    spawn_dir = runtime_root / "spawns" / spawn_id
    assert tuple(spawn_dir.glob("*.flock")) == ()
    assert launch_boundary_lock_path(runtime_root, spawn_id) == (
        runtime_root / "locks" / "launch-boundary" / f"{spawn_id}.lock"
    )
    assert launch_boundary_lock_path(runtime_root, spawn_id).is_file()


def test_late_launch_observation_does_not_recreate_deleted_spawn(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    spawn_dir = runtime_root / "spawns" / spawn_id
    _start_test_spawn(runtime_root, spawn_id)

    assert delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda record: record is not None,
    )

    record_launch_boundary_event(runtime_root, spawn_id, event="worker_boot")

    assert not spawn_dir.exists()


def test_launch_observation_and_spawn_deletion_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"
    spawn_dir = runtime_root / "spawns" / spawn_id
    _start_test_spawn(runtime_root, spawn_id)

    append_started = threading.Event()
    release_append = threading.Event()
    deletion_finished = threading.Event()
    errors: list[BaseException] = []
    deletion_results: list[bool] = []
    original_append_event = launch_boundary_module.append_event

    def paused_append_event(*args, **kwargs) -> None:
        append_started.set()
        if not release_append.wait(timeout=5):
            raise TimeoutError("launch-boundary append was not released")
        original_append_event(*args, **kwargs)

    monkeypatch.setattr(launch_boundary_module, "append_event", paused_append_event)

    def record_observation() -> None:
        try:
            record_launch_boundary_event(runtime_root, spawn_id, event="worker_boot")
        except BaseException as exc:
            errors.append(exc)

    def delete_spawn() -> None:
        try:
            deletion_results.append(
                delete_published_spawn(
                    runtime_root,
                    spawn_id,
                    can_delete=lambda record: record is not None,
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            deletion_finished.set()

    writer = threading.Thread(target=record_observation)
    deleter = threading.Thread(target=delete_spawn)
    writer.start()
    assert append_started.wait(timeout=5)
    deleter.start()
    try:
        assert not deletion_finished.wait(timeout=0.2)
    finally:
        release_append.set()
        writer.join(timeout=5)
        deleter.join(timeout=5)

    assert not writer.is_alive()
    assert not deleter.is_alive()
    assert errors == []
    assert deletion_results == [True]
    assert not spawn_dir.exists()
