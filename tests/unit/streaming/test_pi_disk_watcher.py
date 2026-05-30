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


def test_try_adopt_candidate_stops_for_settled_non_child_spawn(tmp_path: Path) -> None:
    parent_id = SpawnId("p-parent")
    standalone_dir = tmp_path / "spawns" / "p-standalone"
    standalone_dir.mkdir(parents=True)
    _write_json(
        standalone_dir / "state.json",
        {"id": "p-standalone", "status": "running"},
    )

    watcher = PiDiskWatcher(tmp_path, parent_id)
    assert watcher._try_adopt_candidate("p-standalone", standalone_dir) is True
    assert "p-standalone" not in watcher._child_spawn_ids


def test_try_adopt_candidate_stops_for_other_parent_spawn(tmp_path: Path) -> None:
    parent_id = SpawnId("p-parent")
    other_child_dir = tmp_path / "spawns" / "p-other-child"
    other_child_dir.mkdir(parents=True)
    _write_json(
        other_child_dir / "state.json",
        {"id": "p-other-child", "parent_id": "p-other-parent", "status": "running"},
    )

    watcher = PiDiskWatcher(tmp_path, parent_id)
    assert watcher._try_adopt_candidate("p-other-child", other_child_dir) is True
    assert "p-other-child" not in watcher._child_spawn_ids


def test_try_adopt_candidate_keeps_watching_until_state_settles(tmp_path: Path) -> None:
    parent_id = SpawnId("p-parent")
    candidate_dir = tmp_path / "spawns" / "p-new"
    candidate_dir.mkdir(parents=True)

    watcher = PiDiskWatcher(tmp_path, parent_id)
    assert watcher._try_adopt_candidate("p-new", candidate_dir) is False

    _write_json(candidate_dir / "state.json", {"id": "p-new", "status": "running"})
    assert watcher._try_adopt_candidate("p-new", candidate_dir) is True
    assert "p-new" not in watcher._child_spawn_ids


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
