# qa-validated: pi-rpc-quiescence
"""Pi disk-backed quiescence watcher tests."""

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
@pytest.mark.parametrize("entry_name", [".staging", ".p2", "spawn-stage"])
async def test_child_discovery_ignores_non_spawn_row_entries(
    tmp_path: Path,
    entry_name: str,
) -> None:
    parent_id = SpawnId("p1")
    _write_json(
        tmp_path / "spawns" / entry_name / "state.json",
        {"id": entry_name, "parent_id": str(parent_id), "status": "running"},
    )

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        assert watcher.has_pending_child_spawns() is False
        assert watcher.pending_child_spawn_count() == 0
        assert watcher.pending_confirmed_child_ids() == ()
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_child_state_read_failure_recovers_without_losing_child(tmp_path: Path) -> None:
    parent_id = SpawnId("p1")
    state_path = tmp_path / "spawns" / "p2" / "state.json"
    _write_json(
        state_path,
        {"id": "p2", "parent_id": str(parent_id), "status": "running"},
    )
    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        state_path.write_text("{not json", encoding="utf-8")
        await watcher.force_rescan()

        assert watcher.evidence_failure() is not None
        assert watcher.pending_confirmed_child_ids() == ("p2",)

        _write_json(
            state_path,
            {"id": "p2", "parent_id": str(parent_id), "status": "succeeded"},
        )
        await watcher.force_rescan()

        assert watcher.evidence_failure() is None
        assert watcher.has_pending_child_spawns() is False
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


def test_discover_only_finds_own_children(tmp_path: Path) -> None:
    """_discover_child_spawns skips dirs with wrong or missing parent_id."""
    parent_id = SpawnId("p1")
    spawns_dir = tmp_path / "spawns"

    _write_json(
        spawns_dir / "p2" / "state.json",
        {"id": "p2", "parent_id": "p1", "status": "running"},
    )
    _write_json(
        spawns_dir / "p3" / "state.json",
        {"id": "p3", "status": "running"},
    )
    _write_json(
        spawns_dir / "p4" / "state.json",
        {"id": "p4", "parent_id": "p9", "status": "running"},
    )

    watcher = PiDiskWatcher(tmp_path, parent_id)
    watcher._discover_child_spawns()

    assert set(watcher._child_spawns) == {"p2"}


def test_discover_skips_terminal_children_in_count(tmp_path: Path) -> None:
    """_scan_pending_child_spawn_count excludes terminal children."""
    parent_id = SpawnId("p1")
    spawns_dir = tmp_path / "spawns"

    _write_json(
        spawns_dir / "p2" / "state.json",
        {"id": "p2", "parent_id": "p1", "status": "running"},
    )
    _write_json(
        spawns_dir / "p3" / "state.json",
        {"id": "p3", "parent_id": "p1", "status": "succeeded"},
    )

    watcher = PiDiskWatcher(tmp_path, parent_id)
    watcher._discover_child_spawns()
    count = watcher._scan_pending_child_spawn_count()

    assert set(watcher._child_spawns) == {"p2", "p3"}
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
async def test_wait_for_change_polls_late_child_state_after_directory_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.streaming.disk_watcher._PENDING_DISK_POLL_INTERVAL_SECONDS",
        0.05,
    )
    parent_id = SpawnId("p122")
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
            {"id": "p123", "parent_id": str(parent_id), "status": "running"},
        )
        await watcher.force_rescan()
        assert watcher.has_pending_child_spawns() is True
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_force_rescan_tracks_unresolved_child_directory_as_pending(
    tmp_path: Path,
) -> None:
    parent_id = SpawnId("p122")
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
async def test_start_ignores_older_unresolved_dir_but_tracks_newer_child_race(
    tmp_path: Path,
) -> None:
    parent_id = SpawnId("p2984")
    spawns_dir = tmp_path / "spawns"
    preexisting_dir = spawns_dir / "p2983"
    preexisting_dir.mkdir(parents=True)
    (preexisting_dir / "state.lock").touch()

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        assert watcher.has_pending_child_spawns() is False
        assert watcher.pending_child_spawn_count() == 0

        new_child_dir = spawns_dir / "p2985"
        new_child_dir.mkdir()
        (new_child_dir / "state.lock").touch()
        await watcher.force_rescan()

        assert watcher.has_pending_child_spawns() is True
        assert watcher.pending_child_spawn_count() == 1

        _write_json(
            new_child_dir / "state.json",
            {"id": "p2985", "parent_id": str(parent_id), "status": "running"},
        )
        await watcher.force_rescan()

        assert watcher.has_pending_child_spawns() is True
        assert watcher.pending_child_spawn_count() == 1
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_unresolved_child_candidate_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_now = [100.0]
    monkeypatch.setattr(
        "meridian.lib.streaming.disk_watcher.time.monotonic",
        lambda: monotonic_now[0],
    )
    monkeypatch.setattr(
        "meridian.lib.streaming.disk_watcher.time.time",
        lambda: 10_000_000.0,
    )
    parent_id = SpawnId("p2984")
    child_dir = tmp_path / "spawns" / "p2985"

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        child_dir.mkdir()
        await watcher.force_rescan()
        assert watcher.pending_child_spawn_count() == 1

        # Wall-clock changes do not affect the candidate's lifetime.
        monkeypatch.setattr(
            "meridian.lib.streaming.disk_watcher.time.time",
            lambda: -10_000_000.0,
        )
        await watcher.force_rescan()
        assert watcher.pending_child_spawn_count() == 1

        monotonic_now[0] = 131.0
        await watcher.force_rescan()
        assert watcher.pending_child_spawn_count() == 0

        # An expired directory stays known and is not admitted again on rescan.
        monotonic_now[0] = 132.0
        await watcher.force_rescan()
        assert watcher.pending_child_spawn_count() == 0
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_rejected_unresolved_child_clears_stale_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_now = [100.0]
    monkeypatch.setattr(
        "meridian.lib.streaming.disk_watcher.time.monotonic",
        lambda: monotonic_now[0],
    )
    parent_id = SpawnId("p2984")
    state_path = tmp_path / "spawns" / "p2985" / "state.json"

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        state_path.parent.mkdir()
        await watcher.force_rescan()

        state_path.write_text("{not json", encoding="utf-8")
        await watcher.force_rescan()

        assert watcher.evidence_failure() == EvidenceFailure(
            code="pi_private_work_read_failed",
            detail=f"{state_path}: invalid JSON",
        )

        monotonic_now[0] = 131.0
        await watcher.force_rescan()
        _write_json(
            state_path,
            {"id": "p2985", "parent_id": str(parent_id), "status": "succeeded"},
        )
        await watcher.force_rescan()

        assert watcher.evidence_failure() is None
        assert watcher.has_pending_child_spawns() is False
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_force_rescan_tracks_empty_child_state_as_unresolved(
    tmp_path: Path,
) -> None:
    parent_id = SpawnId("p122")
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
    parent_id = SpawnId("p122")
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
    parent_id = SpawnId("p1")
    child_state = tmp_path / "spawns" / "p2" / "state.json"
    _write_json(
        child_state,
        {"id": "p2", "parent_id": str(parent_id), "status": "running"},
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
            {"id": "p2", "parent_id": str(parent_id), "status": "succeeded"},
        )
        await wait_task
        assert watcher.has_pending_child_spawns() is False
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_pi_disk_watcher_tracks_child_spawn_state_from_disk(tmp_path: Path) -> None:
    parent_id = SpawnId("p1")
    child_state = tmp_path / "spawns" / "p2" / "state.json"
    _write_json(child_state, {"id": "p2", "parent_id": str(parent_id), "status": "running"})

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        assert watcher.has_pending_child_spawns() is True

        _write_json(
            child_state,
            {"id": "p2", "parent_id": str(parent_id), "status": "succeeded"},
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
        assert watcher._child_spawns == {}
        assert watcher.has_pending_child_spawns() is False
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_candidate_resolves_to_wrong_parent_is_discarded(tmp_path: Path) -> None:
    """Candidate dir whose state.json resolves to a different parent is discarded, not pending."""
    parent_id = SpawnId("p122")
    child_dir = tmp_path / "spawns" / "p123"

    watcher = PiDiskWatcher(tmp_path, parent_id)
    await watcher.start()
    try:
        # A new directory without state.json qualifies as a candidate while its
        # atomic state write is still pending.
        child_dir.mkdir(parents=True)
        await watcher.force_rescan()

        # Candidate should be tracked as pending while state is unresolved.
        assert watcher.has_pending_child_spawns() is True
        assert watcher.pending_child_spawn_count() == 1

        # Now state.json appears but claims a different parent entirely.
        _write_json(
            child_dir / "state.json",
            {"id": "p123", "parent_id": "p-unrelated-parent", "status": "running"},
        )
        await watcher.force_rescan()

        # Candidate must not be pending — it belongs to another parent.
        # It stays as a rejected tombstone to prevent re-admission.
        assert watcher.has_pending_child_spawns() is False
        assert watcher.pending_child_spawn_count() == 0
    finally:
        await watcher.stop()
