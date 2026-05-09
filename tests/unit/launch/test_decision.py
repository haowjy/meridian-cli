from __future__ import annotations

import pytest

from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.ids import HarnessId
from meridian.lib.harness.semantics import terminal_outcome


def _event(
    *,
    harness_id: str,
    event_type: str,
    payload: dict[str, object] | None = None,
) -> HarnessEvent:
    return HarnessEvent(
        event_type=event_type,
        payload=payload or {},
        harness_id=harness_id,
    )


@pytest.mark.unit
def test_terminal_outcome_codex_turn_completed_succeeds() -> None:
    outcome = terminal_outcome(
        _event(harness_id=HarnessId.CODEX.value, event_type="turn/completed")
    )

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0
    assert outcome.error is None


@pytest.mark.unit
def test_terminal_outcome_claude_result_success_succeeds() -> None:
    outcome = terminal_outcome(
        _event(
            harness_id=HarnessId.CLAUDE.value,
            event_type="result",
            payload={"is_error": False, "subtype": "success", "terminal_reason": "completed"},
        )
    )

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0
    assert outcome.error is None


@pytest.mark.unit
def test_terminal_outcome_claude_result_error_fails() -> None:
    outcome = terminal_outcome(
        _event(
            harness_id=HarnessId.CLAUDE.value,
            event_type="result",
            payload={"is_error": True, "error": "boom"},
        )
    )

    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert outcome.error == "boom"


@pytest.mark.unit
def test_terminal_outcome_opencode_session_idle_succeeds() -> None:
    outcome = terminal_outcome(
        _event(harness_id=HarnessId.OPENCODE.value, event_type="session.idle")
    )

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0


@pytest.mark.unit
def test_terminal_outcome_opencode_session_error_fails() -> None:
    outcome = terminal_outcome(
        _event(
            harness_id=HarnessId.OPENCODE.value,
            event_type="session.error",
            payload={"properties": {"message": "bad state"}},
        )
    )

    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert outcome.error == '{"message": "bad state"}'


@pytest.mark.unit
def test_terminal_outcome_connection_closed_fails() -> None:
    outcome = terminal_outcome(
        _event(
            harness_id=HarnessId.CODEX.value,
            event_type="error/connectionClosed",
        )
    )

    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert outcome.error == "connection_closed"


@pytest.mark.unit
def test_terminal_outcome_unknown_event_returns_none() -> None:
    outcome = terminal_outcome(
        _event(harness_id=HarnessId.CODEX.value, event_type="message/delta")
    )

    assert outcome is None
