import pytest

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.semantics import (
    TerminalEventOutcome,
    TerminalOutcomeCause,
    connection_closed_outcome,
)


@pytest.mark.parametrize(
    ("exit_code", "error"),
    [
        (1, None),
        (0, "reported error"),
    ],
)
def test_succeeded_terminal_outcome_rejects_failure_evidence(
    exit_code: int,
    error: str | None,
) -> None:
    with pytest.raises(
        ValueError,
        match="succeeded terminal outcomes require exit_code=0 and no error",
    ):
        TerminalEventOutcome(status="succeeded", exit_code=exit_code, error=error)


def test_connection_closed_outcome_includes_backend_diagnostics() -> None:
    outcome = connection_closed_outcome(
        RawHarnessEvent(
            event_type="meridian/error/connectionClosed",
            payload={
                "message": "transport closed",
                "backend_exit_code": 23,
                "backend_stderr_excerpt": "fatal vendor detail",
            },
            harness_id="codex",
        ),
        cause=TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE,
    )

    assert outcome.error == (
        "transport closed\n\n"
        "backend exit code: 23\n\n"
        "backend stderr:\nfatal vendor detail"
    )
    assert outcome.cause is TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE
