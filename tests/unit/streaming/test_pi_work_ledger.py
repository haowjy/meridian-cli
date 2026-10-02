from pathlib import Path

from meridian.lib.harness.pi_private_state import BashEvidence
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger


def test_blocker_snapshot_is_immutable_point_in_time() -> None:
    ledger = PiPrivateWorkLedger()
    ledger.update_disk_evidence(
        bash=(
            BashEvidence(
                bash_id="b1",
                status="running",
                is_tracked=True,
                is_background=True,
                command="echo result",
                cwd="/tmp",
                pid=None,
                exit_code=None,
                started_at_ms=0.0,
                ended_at_ms=None,
                log_path="unused",
                stdout_log_path="unused",
                stderr_log_path="unused",
                log_bytes=0,
                timeout_min=1.0,
                originating_bash_id=None,
            ),
        ),
        consumed=frozenset(),
        unobserved_messages=frozenset({"message-1"}),
    )
    snapshot = ledger.blocker_snapshot()
    ledger.update_disk_evidence(bash=(), consumed=frozenset(), unobserved_messages=frozenset())
    assert snapshot.tracked_bash_bg
    assert snapshot.pending_disk_notification
    assert tuple((item.kind, item.code) for item in snapshot.blockers) == (
        ("tracked_bash", "pi_tracked_bash_bg"),
        ("disk_notification", "pi_delivery_event_pending"),
    )


def test_read_failure_is_exposed_through_snapshot() -> None:
    ledger = PiPrivateWorkLedger()
    path = Path("pi-bash/p1/bash-records.json")
    assert ledger.record_read_failure(path, "invalid JSON")
    assert not ledger.record_read_failure(path, "invalid JSON")
    failure = ledger.blocker_snapshot().failure
    assert failure is not None
    assert failure.code == "pi_private_work_read_failed"
    assert failure.detail == "pi-bash/p1/bash-records.json: invalid JSON"
    assert ledger.clear_read_failure(path)
    assert ledger.blocker_snapshot().failure is None
