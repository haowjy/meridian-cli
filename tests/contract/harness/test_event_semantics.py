"""Cross-harness contracts for raw event semantic normalization."""

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.bundle import get_bundle_registry
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.semantics import (
    MERIDIAN_CONNECTION_CLOSED_EVENT,
    normalize_event,
)


def _event(harness_id: HarnessId, event_type: str) -> RawHarnessEvent:
    return RawHarnessEvent(
        harness_id=harness_id.value,
        event_type=event_type,
        payload={"unrecognized_upstream_field": object()},
    )


def test_claude_tool_call_does_not_inherit_opencode_semantics() -> None:
    assert normalize_event(_event(HarnessId.CLAUDE, "tool_call")).semantics.activity is None


def test_opencode_turn_started_does_not_inherit_codex_semantics() -> None:
    assert normalize_event(_event(HarnessId.OPENCODE, "turn/started")).semantics.activity is None


def test_same_event_name_is_classified_independently_by_harness_id() -> None:
    claude_event = _event(HarnessId.CLAUDE, "tool_call")
    opencode_event = _event(HarnessId.OPENCODE, "tool_call")

    assert normalize_event(claude_event).semantics.activity is None
    assert normalize_event(opencode_event).semantics.activity == "turn_active"


def test_unknown_upstream_event_remains_a_valid_open_transport_envelope() -> None:
    event = _event(HarnessId.CODEX, "future/vendor-event")

    assert event.event_type == "future/vendor-event"
    assert event.payload["unrecognized_upstream_field"] is not None
    assert normalize_event(event).semantics.activity is None
    assert normalize_event(event).semantics.terminal is None


def test_every_bundle_reserves_meridian_connection_close_semantics() -> None:
    for bundle in get_bundle_registry().values():
        assert MERIDIAN_CONNECTION_CLOSED_EVENT in bundle.semantics.events
        normalized = normalize_event(
            _event(bundle.harness_id, MERIDIAN_CONNECTION_CLOSED_EVENT)
        )
        assert normalized.semantics.terminal is not None
