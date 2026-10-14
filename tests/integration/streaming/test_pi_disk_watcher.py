"""Validated private-file boundaries and wake delivery with real disk writes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.streaming.disk_watcher import PiDiskWatcher
from tests.support.pi import write_pi_bash_record


@pytest.mark.parametrize("bytes_value", [b"{not json", b"\xff", b'{"records": []}'])
@pytest.mark.asyncio
async def test_hard_private_read_failure_recovers_only_after_valid_publication(
    tmp_path: Path, bytes_value: bytes
) -> None:
    parent_id = SpawnId("p-parent")
    file = tmp_path / "pi-bash" / str(parent_id) / "bash-records.json"
    file.parent.mkdir(parents=True)
    file.write_bytes(bytes_value)
    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        failure = watcher.evidence_failure()
        assert failure is not None and failure.code == "pi_private_work_read_failed"
        assert str(file) in (failure.detail or "")
        write_pi_bash_record(tmp_path, parent_id)
        await watcher.force_rescan()
        assert watcher.evidence_failure() is None and watcher.has_tracked_bash_bg()
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_missing_private_file_is_empty(tmp_path: Path) -> None:
    watcher = PiDiskWatcher(tmp_path, SpawnId("p-parent"))
    await watcher.start()
    try:
        assert watcher.evidence_failure() is None
    finally:
        await watcher.stop()


@pytest.mark.parametrize("before_wait", [False, True])
@pytest.mark.asyncio
async def test_validated_disk_change_wakes_current_or_next_waiter(
    tmp_path: Path, before_wait: bool
) -> None:
    parent_id = SpawnId("p-parent")
    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        waiter = None if before_wait else asyncio.create_task(watcher.wait_for_change())
        await asyncio.sleep(0)
        write_pi_bash_record(tmp_path, parent_id)
        await watcher.force_rescan()
        if waiter is None:
            waiter = asyncio.create_task(watcher.wait_for_change())
        await asyncio.wait_for(waiter, 1)
        assert watcher.has_tracked_bash_bg()
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_retired_marker_cannot_poison_or_authorize_completion(tmp_path: Path) -> None:
    parent = tmp_path / "pi-bash" / "p-parent"
    parent.mkdir(parents=True)
    (parent / "last-notification.json").write_text("{invalid retired marker")
    watcher = PiDiskWatcher(tmp_path, SpawnId("p-parent"))
    await watcher.start()
    try:
        assert watcher.evidence_failure() is None
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_new_run_uses_shared_child_ledger_but_ignores_old_bash(tmp_path: Path) -> None:
    import json

    from meridian.lib.core.refs import parse_ref

    write_pi_bash_record(tmp_path, SpawnId("p2"))
    parent = tmp_path / "pi-bash" / "p2"
    (parent / "cleared-spawns.json").write_text(json.dumps({
        "v": 1, "spawn_id": "p2", "updated_at_ms": 0, "cleared_spawn_ids": ["p5"],
    }))
    watcher = PiDiskWatcher(tmp_path, parse_ref("p2.3", row_key=True))
    await watcher.start()
    try:
        assert watcher.evidence_failure() is None
        assert not watcher.has_tracked_bash_bg()
        # A malformed shared ledger is visible to the new run, not silently
        # replaced by an empty run-local ledger.
        (parent / "cleared-spawns.json").write_text("{broken")
        await watcher.force_rescan()
        assert watcher.evidence_failure() is not None
    finally:
        await watcher.stop()
