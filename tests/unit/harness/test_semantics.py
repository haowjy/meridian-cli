import pytest

from meridian.lib.harness.semantics import TerminalEventOutcome


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
