"""Cancellation outcome projection into public spawn output."""

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_service import CancelOutcome
from meridian.lib.ops.spawn import api as spawn_api


def test_spawn_cancel_reports_deadline_outcome_as_finalizing() -> None:
    output = spawn_api._spawn_cancel_output_from_outcome(
        CancelOutcome(
            spawn_id="p-held",
            status=SpawnStatus.FINALIZING,
            origin="cancel",
            exit_code=1,
            launch_number=1,
        )
    )

    assert output.status == "finalizing"
    assert output.exit_code == 1
    assert output.message == "Spawn did not terminate within grace; reaper will reconcile."
