"""Launch-boundary storage and lock placement regressions."""

from pathlib import Path

from meridian.lib.state.launch_boundary import (
    launch_boundary_lock_path,
    record_launch_boundary_event,
)


def test_lock_lives_outside_spawn_dir(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = "p1"

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
