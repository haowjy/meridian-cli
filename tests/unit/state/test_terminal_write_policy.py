from __future__ import annotations

import pytest

from meridian.lib.state.spawn.terminal_policy import decide_terminal_write


@pytest.mark.parametrize("incoming_origin", ["runner", "reconciler"])
def test_terminal_write_policy_rejects_missing_rows(incoming_origin: str) -> None:
    decision = decide_terminal_write(
        current_status=None,
        current_terminal_origin=None,
        incoming_origin=incoming_origin,
    )

    assert decision.disposition == "reject"


@pytest.mark.parametrize("current_status", ["queued", "running", "finalizing"])
def test_terminal_write_policy_appends_authoritative_finalizes_for_active_rows(
    current_status: str,
) -> None:
    decision = decide_terminal_write(
        current_status=current_status,
        current_terminal_origin=None,
        incoming_origin="runner",
    )

    assert decision.disposition == "append"


@pytest.mark.parametrize("current_status", ["queued", "running", "finalizing"])
def test_terminal_write_policy_appends_reconciler_finalizes_for_active_rows(
    current_status: str,
) -> None:
    decision = decide_terminal_write(
        current_status=current_status,
        current_terminal_origin=None,
        incoming_origin="reconciler",
    )

    assert decision.disposition == "append"


def test_terminal_write_policy_replaces_reconciler_terminal_with_authoritative_origin() -> None:
    decision = decide_terminal_write(
        current_status="failed",
        current_terminal_origin="reconciler",
        incoming_origin="runner",
    )

    assert decision.disposition == "replace"


def test_terminal_write_policy_rejects_authoritative_terminal_loser() -> None:
    decision = decide_terminal_write(
        current_status="failed",
        current_terminal_origin="runner",
        incoming_origin="launcher",
    )

    assert decision.disposition == "reject"


def test_terminal_write_policy_rejects_reconciler_after_authoritative_terminal() -> None:
    decision = decide_terminal_write(
        current_status="failed",
        current_terminal_origin="runner",
        incoming_origin="reconciler",
    )

    assert decision.disposition == "reject"


def test_terminal_write_policy_rejects_reconciler_after_reconciler_terminal() -> None:
    decision = decide_terminal_write(
        current_status="failed",
        current_terminal_origin="reconciler",
        incoming_origin="reconciler",
    )

    assert decision.disposition == "reject"
