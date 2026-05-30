# qa-validated: pi-rpc-quiescence
"""Pi disk-backed quiescence watcher tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.streaming.disk_watcher import PiDiskWatcher


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_discover_only_finds_own_children(tmp_path: Path) -> None:
    """_discover_child_spawns skips dirs with wrong or missing parent_id."""
    parent_id = SpawnId("p-parent")
    spawns_dir = tmp_path / "spawns"

    # Our child.
    _write_json(
        spawns_dir / "p-child" / "state.json",
        {"id": "p-child", "parent_id": "p-parent", "status": "running"},
    )
    # Standalone spawn (no parent_id).
    _write_json(
        spawns_dir / "p-standalone" / "state.json",
        {"id": "p-standalone", "status": "running"},
    )
    # Another parent's child.
    _write_json(
        spawns_dir / "p-other" / "state.json",
        {"id": "p-other", "parent_id": "p-other-parent", "status": "running"},
    )

    watcher = PiDiskWatcher(tmp_path, parent_id)
    watcher._discover_child_spawns()

    assert watcher._child_spawn_ids == {"p-child"}


def test_discover_skips_terminal_children_in_count(tmp_path: Path) -> None:
    """_scan_pending_child_spawn_count excludes terminal children."""
    parent_id = SpawnId("p-parent")
    spawns_dir = tmp_path / "spawns"

    _write_json(
        spawns_dir / "p-running" / "state.json",
        {"id": "p-running", "parent_id": "p-parent", "status": "running"},
    )
    _write_json(
        spawns_dir / "p-done" / "state.json",
        {"id": "p-done", "parent_id": "p-parent", "status": "succeeded"},
    )

    watcher = PiDiskWatcher(tmp_path, parent_id)
    watcher._discover_child_spawns()
    count = watcher._scan_pending_child_spawn_count()

    assert watcher._child_spawn_ids == {"p-running", "p-done"}
    assert count == 1


@pytest.mark.asyncio
async def test_pi_disk_watcher_tracks_child_spawn_state_from_disk(tmp_path: Path) -> None:
    parent_id = SpawnId("p-parent")
    child_state = tmp_path / "spawns" / "p-child" / "state.json"
    _write_json(child_state, {"id": "p-child", "parent_id": str(parent_id), "status": "running"})

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        assert watcher.has_pending_child_spawns() is True

        _write_json(
            child_state,
            {"id": "p-child", "parent_id": str(parent_id), "status": "succeeded"},
        )
        await watcher.force_rescan()

        assert watcher.has_pending_child_spawns() is False
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_pi_disk_watcher_tracks_bash_and_notification_files(tmp_path: Path) -> None:
    parent_id = SpawnId("p-parent")
    bash_dir = tmp_path / "pi-bash" / str(parent_id)
    records_path = bash_dir / "bash-records.json"
    marker_path = bash_dir / "last-notification.json"
    _write_json(
        records_path,
        {
            "records": {
                "b1": {
                    "bash_id": "b1",
                    "is_tracked": True,
                    "is_background": True,
                    "status": "running",
                }
            }
        },
    )
    _write_json(marker_path, {"ts_epoch_secs": 123.5})

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        assert watcher.has_tracked_bash_bg() is True
        assert watcher.last_notification_ts() == 123.5

        _write_json(
            records_path,
            {
                "records": {
                    "b1": {
                        "bash_id": "b1",
                        "is_tracked": True,
                        "is_background": True,
                        "status": "exited",
                    }
                }
            },
        )
        await watcher.force_rescan()

        assert watcher.has_tracked_bash_bg() is False
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_no_watcher_tasks_for_non_child_spawns(tmp_path: Path) -> None:
    """After start, only spawns_dir and bash_dir watchers exist — no per-child tasks."""
    parent_id = SpawnId("p-parent")
    spawns_dir = tmp_path / "spawns"

    # Create 10 standalone spawn dirs — none are children.
    for i in range(10):
        _write_json(
            spawns_dir / f"p-other-{i}" / "state.json",
            {"id": f"p-other-{i}", "status": "succeeded"},
        )

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        # Only 2 background tasks: spawns_dir watcher + bash_dir watcher.
        assert len(watcher._tasks) == 2
        assert watcher._child_spawn_ids == set()
        assert watcher.has_pending_child_spawns() is False
    finally:
        await watcher.stop()
