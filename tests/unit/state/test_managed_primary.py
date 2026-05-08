from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.state.managed_primary import ManagedPrimaryCausalTracker


def _event(event_type: str, payload: dict[str, object]) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, payload=payload, harness_id="codex")


def test_causal_tracker_carries_turn_request_item_correlation() -> None:
    tracker = ManagedPrimaryCausalTracker()

    first = tracker.derive(_event("turn/started", {"turnId": "turn-1"}))
    request_opened = tracker.derive(_event("request/opened", {"request_id": "req-1"}))
    item_started = tracker.derive(_event("item/started", {"item_id": "item-1"}))
    request_closed = tracker.derive(_event("request/resolved", {"request_id": "req-1"}))

    assert first.turn_id == "turn-1"
    assert request_opened.turn_id == "turn-1"
    assert item_started.turn_id == "turn-1"
    assert request_closed.turn_id == "turn-1"
    assert request_closed.request_id == "req-1"
    assert request_closed.stale_after_interrupt is False
    assert request_closed.interrupt_epoch == 0


def test_causal_tracker_marks_late_old_turn_output_stale_after_interrupt() -> None:
    tracker = ManagedPrimaryCausalTracker()

    tracker.derive(_event("turn/started", {"turnId": "turn-old"}))
    tracker.derive(_event("item/started", {"item_id": "item-old"}))
    interrupt = tracker.derive(
        _event(
            "control/interrupt/requested",
            {"kind": "interrupt", "status": "requested", "turn_id": "turn-old"},
        )
    )
    tracker.derive(_event("turn/started", {"turnId": "turn-new"}))
    late_item = tracker.derive(_event("item/completed", {"item_id": "item-old"}))

    assert interrupt.interrupt_epoch == 1
    assert interrupt.turn_id == "turn-old"
    assert late_item.turn_id == "turn-old"
    assert late_item.item_id == "item-old"
    assert late_item.interrupt_epoch == 1
    assert late_item.stale_after_interrupt is True


def test_causal_tracker_respects_explicit_interrupt_fields() -> None:
    tracker = ManagedPrimaryCausalTracker()

    fields = tracker.derive(
        _event(
            "item/completed",
            {
                "interrupt_epoch": 7,
                "stale_after_interrupt": True,
                "turn_id": "turn-explicit",
                "item_id": "item-explicit",
                "request_id": "request-explicit",
            },
        )
    )

    assert fields.interrupt_epoch == 7
    assert fields.stale_after_interrupt is True
    assert fields.turn_id == "turn-explicit"
    assert fields.item_id == "item-explicit"
    assert fields.request_id == "request-explicit"
