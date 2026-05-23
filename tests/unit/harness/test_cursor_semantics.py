"""Cursor terminal semantics tests."""

from __future__ import annotations

from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.semantics import clears_signal, terminal_outcome


def _cursor_event(payload: dict[str, object]) -> HarnessEvent:
    return HarnessEvent(event_type="result", harness_id="cursor", payload=payload)


def test_cursor_result_success_outcome() -> None:
    outcome = terminal_outcome(_cursor_event({"type": "result", "subtype": "success"}))

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0


def test_cursor_result_without_subtype_defaults_to_success() -> None:
    outcome = terminal_outcome(_cursor_event({"type": "result"}))

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0


def test_cursor_result_error_outcome() -> None:
    outcome = terminal_outcome(
        _cursor_event({"type": "result", "is_error": True, "error": "quota exceeded"})
    )

    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error == "quota exceeded"


def test_cursor_result_non_success_subtype_fails() -> None:
    outcome = terminal_outcome(_cursor_event({"type": "result", "subtype": "max_turns"}))

    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error == "cursor_result_max_turns"


def test_cursor_result_clears_signal() -> None:
    assert clears_signal(_cursor_event({"type": "result"})) is True
