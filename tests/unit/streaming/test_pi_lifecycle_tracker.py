"""Focused contracts for retained Pi lifecycle validation and deduplication."""

from meridian.lib.streaming.pi_lifecycle_tracker import PiLifecycleTracker
from tests.support.pi import pi_event


def test_ordinary_pi_event_does_not_affect_lifecycle_tracking() -> None:
    tracker = PiLifecycleTracker.empty()

    assert tracker.observe(pi_event("message_start")) is False
    assert tracker.lifecycle_tracking_invalidated_error is None
    assert tracker.canonical_event_keys == set()


def test_malformed_quiescence_ready_schema_invalidates_tracking() -> None:
    tracker = PiLifecycleTracker.empty()

    duplicate = tracker.observe(
        pi_event(
            "meridian.quiescence.ready",
            {"schema_version": 2, "correlation_id": "ready-1"},
        )
    )

    assert duplicate is False
    assert tracker.lifecycle_tracking_invalidated_error == (
        "pi_lifecycle_tracking_invalidated:unsupported_schema_version:2"
    )
    assert tracker.canonical_event_keys == set()


def test_valid_quiescence_ready_is_deduplicated_by_correlation_id() -> None:
    tracker = PiLifecycleTracker.empty()
    event = pi_event(
        "meridian.quiescence.ready",
        {"schema_version": 1, "correlation_id": "ready-1"},
    )

    assert tracker.observe(event) is False
    assert tracker.observe(event) is True
    assert tracker.lifecycle_tracking_invalidated_error is None
    assert tracker.canonical_event_keys == {("meridian.quiescence.ready", "ready-1")}
