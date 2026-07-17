from pathlib import Path

from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger


def test_blocker_snapshot_is_immutable_point_in_time() -> None:
    ledger = PiPrivateWorkLedger()
    ledger.update_disk_evidence(tracked_bash_bg=True, last_notification_ts=20.0)

    snapshot = ledger.blocker_snapshot(parent_idle_epoch=20.0)

    ledger.update_disk_evidence(tracked_bash_bg=False, last_notification_ts=None)

    assert snapshot.tracked_bash_bg is True
    assert snapshot.pending_disk_notification is True
    assert tuple((item.kind, item.code) for item in snapshot.blockers) == (
        ("tracked_bash", "pi_tracked_bash_bg"),
        ("disk_notification", "pi_disk_notification"),
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
