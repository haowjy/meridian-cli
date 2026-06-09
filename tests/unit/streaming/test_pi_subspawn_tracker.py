# qa-validated: pi-rpc-quiescence
"""Pi subspawn tracker tests."""

from __future__ import annotations

import pytest

from meridian.lib.streaming.pi_subspawn_tracker import PiSubspawnTracker
from tests.unit.streaming.pi_quiescence_test_helpers import pi_event as _pi_event


def test_pi_subspawn_tracker_tracks_only_blocking_children_and_notifications() -> None:
    tracker = PiSubspawnTracker.empty()

    tracker.observe(
        _pi_event(
            "meridian.subspawn.start",
            {"subspawn_id": "detached-1", "wait_policy": "detached"},
        )
    )
    assert tracker.has_pending() is False

    tracker.observe(
        _pi_event(
            "meridian.subspawn.start",
            {"subspawn_id": "tracked-1", "wait_policy": "tracked", "pid": 4401},
        )
    )
    assert tracker.has_pending() is True
    assert tracker.active_tracked_pgid_candidates() == (4401,)

    tracker.observe(_pi_event("meridian.notification.queued", {"notification_id": "n-1"}))
    assert tracker.has_pending_notifications() is True

    tracker.observe(_pi_event("meridian.notification.completed", {"notification_id": "n-1"}))
    assert tracker.has_pending_notifications() is False

    tracker.observe(
        _pi_event(
            "meridian.notification.failed",
            {"notification_id": "n-2", "reason": "sendMessage_error"},
        )
    )
    assert tracker.notification_failure_error == "pi_notification_failed:sendMessage_error"

    tracker.observe(
        _pi_event("meridian.subspawn.end", {"subspawn_id": "tracked-1", "wait_policy": "tracked"})
    )
    assert tracker.has_pending() is False
    assert tracker.active_tracked_pgid_candidates() == ()


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_error"),
    [
        (
            "meridian.subspawn.start",
            {"wait_policy": "tracked"},
            "pi_lifecycle_tracking_invalidated:missing_subspawn_id:meridian.subspawn.start",
        ),
        (
            "meridian.subspawn.start",
            {"schema_version": 2, "subspawn_id": "tracked-1", "wait_policy": "tracked"},
            "pi_lifecycle_tracking_invalidated:unsupported_schema_version:2",
        ),
        (
            "meridian.lifecycle.parse_error",
            {
                "type": "meridian.lifecycle.parse_error",
                "schema_version": 1,
                "reason": "unsupported_schema_version",
                "error": "unsupported_schema_version",
                "raw_type": "meridian.subspawn.start",
                "raw_line": '{"type":"meridian.subspawn.start","schema_version":2}',
            },
            "pi_lifecycle_tracking_invalidated:unsupported_schema_event:meridian.subspawn.start",
        ),
    ],
)
def test_pi_subspawn_tracker_invalidates_malformed_canonical_lifecycle(
    event_type: str,
    payload: dict[str, object],
    expected_error: str,
) -> None:
    tracker = PiSubspawnTracker.empty()

    tracker.observe(_pi_event(event_type, payload))

    assert tracker.has_pending() is False
    assert tracker.has_pending_notifications() is False
    assert tracker.lifecycle_tracking_invalidated_error == expected_error


def test_pi_subspawn_tracker_ignores_noncanonical_parse_diagnostics() -> None:
    tracker = PiSubspawnTracker.empty()

    tracker.observe(
        _pi_event(
            "meridian.lifecycle.parse_error",
            {
                "type": "meridian.lifecycle.parse_error",
                "reason": "missing_type",
                "raw_line": '{"id":"missing-type"}',
            },
        )
    )

    assert tracker.has_pending() is False
    assert tracker.has_pending_notifications() is False
    assert tracker.notification_failure_error is None
    assert tracker.lifecycle_tracking_invalidated_error is None


def test_pi_subspawn_tracker_invalidates_unknown_pi_lifecycle_namespace_events() -> None:
    tracker = PiSubspawnTracker.empty()

    tracker.observe(
        _pi_event(
            "meridian_subspawn_started",
            {"id": "legacy-child", "wait_policy": "tracked", "pid": 4401},
        )
    )

    assert tracker.has_pending() is False
    assert tracker.has_pending_notifications() is False
    assert (
        tracker.lifecycle_tracking_invalidated_error
        == "pi_lifecycle_tracking_invalidated:unsupported_lifecycle_event:"
        "meridian_subspawn_started"
    )


def test_pi_subspawn_tracker_leaves_ordinary_harness_events_unaffected() -> None:
    tracker = PiSubspawnTracker.empty()

    tracker.observe(_pi_event("agent_progress", {"message": "ordinary output"}))

    assert tracker.has_pending() is False
    assert tracker.has_pending_notifications() is False
    assert tracker.notification_failure_error is None
    assert tracker.lifecycle_tracking_invalidated_error is None


def test_pi_subspawn_tracker_deduplicates_canonical_events() -> None:
    tracker = PiSubspawnTracker.empty()

    start = _pi_event(
        "meridian.subspawn.start",
        {
            "schema_version": 1,
            "subspawn_id": "j-dup",
            "correlation_id": "corr-start",
            "wait_policy": "tracked",
            "pid": 4401,
        },
    )
    duplicate_start = _pi_event(
        "meridian.subspawn.start",
        {
            "schema_version": 1,
            "subspawn_id": "j-dup",
            "correlation_id": "corr-start",
            "wait_policy": "tracked",
            "pid": 5501,
        },
    )
    end = _pi_event(
        "meridian.subspawn.end",
        {
            "schema_version": 1,
            "subspawn_id": "j-dup",
            "correlation_id": "corr-end",
            "wait_policy": "tracked",
        },
    )

    assert tracker.observe(start) is False
    assert tracker.observe(duplicate_start) is True
    assert tracker.active_tracked_count() == 1
    assert tracker.active_tracked_pgid_candidates() == (4401,)
    assert tracker.observe(end) is False
    assert tracker.observe(end) is True
    assert tracker.has_pending() is False
    assert tracker.active_tracked_pgid_candidates() == ()

