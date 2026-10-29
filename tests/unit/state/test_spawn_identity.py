"""Continue-chat selection over the spawn record's chat facts."""

from meridian.lib.state.spawn.model import RunBoundaryOutcome, SpawnRecord


def test_spawn_current_chat_precedes_boundary_but_not_mismatch() -> None:
    row = SpawnRecord(
        id="p1", status="running", chat_id="c-old", owner_chat_id="c0", current_chat_id="c-new"
    )
    assert row.continue_chat_id == "c-new"
    terminal = row.model_copy(update={"status": "succeeded"})
    assert terminal.continue_chat_id == "c-new"
    assert terminal.model_copy(update={"record_mode": "historical"}).continue_chat_id is None
    mismatch = row.model_copy(
        update={
            "run_boundary": RunBoundaryOutcome(status="mismatch"),
        }
    )
    assert mismatch.continue_chat_id == "c-old"
