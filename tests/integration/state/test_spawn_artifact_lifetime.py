"""Spawn-owned artifacts cannot outlive their published row."""

from pathlib import Path

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.state.failure_sentinel import write_failure_sentinel
from meridian.lib.state.spawn_aggregate import delete_published_spawn
from meridian.lib.state.spawn_store import start_spawn


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
