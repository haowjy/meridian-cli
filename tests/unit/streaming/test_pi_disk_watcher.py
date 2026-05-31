# qa-validated: pi-rpc-quiescence
"""Pi disk-backed quiescence watcher tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
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

    _write_json(
        spawns_dir / "p-child" / "state.json",
        {"id": "p-child", "parent_id": "p-parent", "status": "running"},
    )
    _write_json(
        spawns_dir / "p-standalone" / "state.json",
        {"id": "p-standalone", "status": "running"},
    )
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
async def test_wait_for_change_wakes_on_refresh(tmp_path: Path) -> None:
    parent_id = SpawnId("p-parent")
    marker_path = tmp_path / "pi-bash" / str(parent_id) / "last-notification.json"
    _write_json(marker_path, {"ts_epoch_secs": 1.0})

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        wait_task = asyncio.create_task(watcher.wait_for_change())
        await asyncio.sleep(0)
        assert not wait_task.done()
        _write_json(marker_path, {"ts_epoch_secs": 2.0})
        await watcher.force_rescan()
        await asyncio.wait_for(wait_task, timeout=1.0)
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_wait_for_change_observes_pre_signaled_refresh(tmp_path: Path) -> None:
    parent_id = SpawnId("p-parent")
    marker_path = tmp_path / "pi-bash" / str(parent_id) / "last-notification.json"

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        _write_json(marker_path, {"ts_epoch_secs": 1.0})
        await watcher.force_rescan()
        await asyncio.wait_for(watcher.wait_for_change(), timeout=1.0)

        wait_task = asyncio.create_task(watcher.wait_for_change())
        await asyncio.sleep(0)
        assert not wait_task.done()
        wait_task.cancel()
        with suppress(asyncio.CancelledError):
            await wait_task
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_wait_for_change_polls_late_child_state_after_directory_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.streaming.disk_watcher._PENDING_DISK_POLL_INTERVAL_SECONDS",
        0.05,
    )
    parent_id = SpawnId("p-parent")
    child_dir = tmp_path / "spawns" / "p123"

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        child_dir.mkdir(parents=True)
        watcher._candidate_child_spawn_ids.add("p123")
        watcher._refresh_cached_state()
        await asyncio.wait_for(watcher.wait_for_change(), timeout=1.0)
        assert watcher.has_pending_child_spawns() is True

        wait_task = asyncio.create_task(watcher.wait_for_change())
        await asyncio.sleep(0)
        assert not wait_task.done()
        _write_json(
            child_dir / "state.json",
            {"id": "p123", "parent_id": str(parent_id), "status": "running"},
        )
        await asyncio.wait_for(wait_task, timeout=1.0)
        assert watcher._child_spawn_ids == {"p123"}
        assert watcher._candidate_child_spawn_ids == set()
        assert watcher.has_pending_child_spawns() is True
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_force_rescan_tracks_unresolved_child_directory_as_pending(
    tmp_path: Path,
) -> None:
    parent_id = SpawnId("p-parent")
    child_dir = tmp_path / "spawns" / "p123"

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        child_dir.mkdir(parents=True)
        await watcher.force_rescan()

        assert watcher.has_pending_child_spawns() is True
        assert watcher.pending_child_spawn_count() == 1

        _write_json(
            child_dir / "state.json",
            {"id": "p123", "parent_id": str(parent_id), "status": "succeeded"},
        )
        await watcher.force_rescan()

        assert watcher.has_pending_child_spawns() is False
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_force_rescan_tracks_empty_child_state_as_unresolved(
    tmp_path: Path,
) -> None:
    parent_id = SpawnId("p-parent")
    child_state = tmp_path / "spawns" / "p123" / "state.json"
    _write_json(child_state, {})

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        assert watcher.has_pending_child_spawns() is True
        assert watcher.pending_child_spawn_count() == 1
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_candidate_child_directory_deleted_clears_pending(
    tmp_path: Path,
) -> None:
    parent_id = SpawnId("p-parent")
    child_dir = tmp_path / "spawns" / "p123"

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        child_dir.mkdir(parents=True)
        await watcher.force_rescan()
        assert watcher.has_pending_child_spawns() is True

        child_dir.rmdir()
        await watcher.force_rescan()
        assert watcher.has_pending_child_spawns() is False
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_force_rescan_ignores_stale_non_allocated_unresolved_dir(
    tmp_path: Path,
) -> None:
    parent_id = SpawnId("p-parent")
    child_dir = tmp_path / "spawns" / "p-child"

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        child_dir.mkdir(parents=True)
        await watcher.force_rescan()

        assert watcher.has_pending_child_spawns() is False
        assert watcher.pending_child_spawn_count() == 0
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_wait_for_change_wakes_on_child_terminal_without_manual_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal child state.json updates are observed via bounded poll, not inotify."""
    monkeypatch.setattr(
        "meridian.lib.streaming.disk_watcher._PENDING_DISK_POLL_INTERVAL_SECONDS",
        0.05,
    )
    parent_id = SpawnId("p-parent")
    child_state = tmp_path / "spawns" / "p-child" / "state.json"
    _write_json(
        child_state,
        {"id": "p-child", "parent_id": str(parent_id), "status": "running"},
    )

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        assert watcher.has_pending_child_spawns() is True
        wait_task = asyncio.create_task(watcher.wait_for_change())
        await asyncio.sleep(0)
        assert not wait_task.done()
        _write_json(
            child_state,
            {"id": "p-child", "parent_id": str(parent_id), "status": "succeeded"},
        )
        await asyncio.wait_for(wait_task, timeout=2.0)
        assert watcher.has_pending_child_spawns() is False
    finally:
        await watcher.stop()


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
    """Non-child spawn dirs must never spawn per-child watcher tasks."""
    parent_id = SpawnId("p-parent")
    spawns_dir = tmp_path / "spawns"

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        baseline_task_count = len(watcher._tasks)

        for i in range(10):
            _write_json(
                spawns_dir / f"p-other-{i}" / "state.json",
                {"id": f"p-other-{i}", "status": "succeeded"},
            )
        await watcher.force_rescan()

        assert len(watcher._tasks) == baseline_task_count
        assert watcher._child_spawn_ids == set()
        assert watcher.has_pending_child_spawns() is False
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_candidate_resolves_to_wrong_parent_is_discarded(tmp_path: Path) -> None:
    """Candidate dir whose state.json resolves to a different parent is discarded, not pending."""
    parent_id = SpawnId("p-parent")
    child_dir = tmp_path / "spawns" / "p123"

    # Directory exists but no state.json yet — numeric name qualifies as a candidate.
    child_dir.mkdir(parents=True)

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        # Candidate should be tracked as pending while state is unresolved.
        assert watcher.has_pending_child_spawns() is True
        assert watcher.pending_child_spawn_count() == 1

        # Now state.json appears but claims a different parent entirely.
        _write_json(
            child_dir / "state.json",
            {"id": "p123", "parent_id": "p-unrelated-parent", "status": "running"},
        )
        await watcher.force_rescan()

        # Candidate must be discarded — it belongs to another parent.
        assert watcher.has_pending_child_spawns() is False
        assert watcher.pending_child_spawn_count() == 0
        assert "p123" not in watcher._candidate_child_spawn_ids
        assert "p123" not in watcher._child_spawn_ids
    finally:
        await watcher.stop()
