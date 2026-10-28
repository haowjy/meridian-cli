"""Cancellation outcome projection into public spawn output."""

from meridian.lib.core.spawn_service import CancelOutcome
from meridian.lib.ops.spawn import api as spawn_api


def test_spawn_cancel_reports_nonconverged_running_outcome_as_failure() -> None:
    output = spawn_api._spawn_cancel_output_from_outcome(
        CancelOutcome(
            spawn_id="p-running",
            status="running",
            origin="cancel",
            exit_code=130,
        )
    )

    assert output.status == "failed"
    assert output.exit_code == 1
    assert output.error == "Spawn remained running after cancellation."
