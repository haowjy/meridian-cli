# qa-validated: pi-rpc-quiescence
"""Pi private-ledger disk watcher tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.streaming.completion_contracts import EvidenceFailure
from meridian.lib.streaming.disk_watcher import PiDiskWatcher


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_hard_private_work_read_error_surfaces_typed_failure(tmp_path: Path) -> None:
    parent_id = SpawnId("p-parent")
    records_path = tmp_path / "pi-bash" / str(parent_id) / "bash-records.json"
    records_path.parent.mkdir(parents=True)
    records_path.write_text("{not json", encoding="utf-8")

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        failure = watcher.evidence_failure()

        assert failure == EvidenceFailure(
            code="pi_private_work_read_failed",
            detail=f"{records_path}: invalid JSON",
        )

        _write_json(records_path, {"records": {}})
        await watcher.force_rescan()

        assert watcher.evidence_failure() is None
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_missing_private_work_file_is_not_an_evidence_failure(tmp_path: Path) -> None:
    watcher = PiDiskWatcher(tmp_path, SpawnId("p-parent"))
    await watcher.start()
    try:
        assert watcher.evidence_failure() is None
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_invalid_utf8_surfaces_typed_failure(tmp_path: Path) -> None:
    parent_id = SpawnId("p-parent")
    records_path = tmp_path / "pi-bash" / str(parent_id) / "bash-records.json"
    records_path.parent.mkdir(parents=True)
    records_path.write_bytes(b"\xff")

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        failure = watcher.evidence_failure()

        assert failure is not None
        assert failure.code == "pi_private_work_read_failed"
        assert failure.detail is not None
        assert str(records_path) in failure.detail
    finally:
        await watcher.stop()


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
        await wait_task
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
        await watcher.wait_for_change()

        wait_task = asyncio.create_task(watcher.wait_for_change())
        await asyncio.sleep(0)
        assert not wait_task.done()
        wait_task.cancel()
        with suppress(asyncio.CancelledError):
            await wait_task
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
