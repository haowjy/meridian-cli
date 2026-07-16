from pathlib import Path

from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger


def test_blocker_snapshot_is_immutable_point_in_time() -> None:
    ledger = PiPrivateWorkLedger()
    ledger.note_subspawn_started("internal-1", process_group_id=4101)
    ledger.note_notification_started(
        "notification-1",
        phase="queued",
        observation_monotonic=10.0,
        notification_timeout_seconds=5.0,
    )
    ledger.update_disk_evidence(
        tracked_bash_bg=True,
        last_notification_ts=20.0,
    )
    ledger.admit_allocation_uncertainty("p9", deadline_monotonic=30.0)

    snapshot = ledger.blocker_snapshot(parent_idle_epoch=20.0)

    ledger.note_subspawn_ended("internal-1")
    ledger.note_notification_ended("notification-1")
    ledger.update_disk_evidence(
        tracked_bash_bg=False,
        last_notification_ts=None,
    )
    ledger.resolve_allocation_uncertainty("p9")

    assert snapshot.tracked_subspawn_ids == ("internal-1",)
    assert snapshot.tracked_bash_bg is True
    assert tuple(item.notification_id for item in snapshot.pending_notifications) == (
        "notification-1",
    )
    assert snapshot.pending_disk_notification is True
    assert snapshot.allocation_uncertainty_ids == ("p9",)
    assert tuple((item.kind, item.code) for item in snapshot.blockers) == (
        ("rowless_subspawn", "pi_tracked_child"),
        ("allocation", "pi_allocation_uncertainty"),
        ("tracked_bash", "pi_tracked_bash_bg"),
        ("notification", "pi_pending_notification"),
        ("disk_notification", "pi_disk_notification"),
    )


def test_cleanup_handles_are_immutable_and_excludable() -> None:
    ledger = PiPrivateWorkLedger()
    ledger.note_subspawn_started("internal-2", process_group_id=4202)
    ledger.note_subspawn_started("internal-1", process_group_id=4201)

    handles = ledger.cleanup_handles(exclude_ids={"internal-2"})

    assert tuple((item.subspawn_id, item.process_group_id) for item in handles) == (
        ("internal-1", 4201),
    )


def test_read_failure_is_exposed_through_snapshot() -> None:
    ledger = PiPrivateWorkLedger()
    path = Path("pi-bash/p1/bash-records.json")

    assert ledger.record_read_failure(path, "invalid JSON") is True
    assert ledger.record_read_failure(path, "invalid JSON") is False

    failure = ledger.blocker_snapshot(parent_idle_epoch=None).failure
    assert failure is not None
    assert failure.code == "pi_private_work_read_failed"
    assert failure.detail == "pi-bash/p1/bash-records.json: invalid JSON"

    assert ledger.clear_read_failure(path) is True
    assert ledger.blocker_snapshot(parent_idle_epoch=None).failure is None
