"""Retention never mistakes a failed reservation for a bound continuation."""

from meridian.lib.core.refs import parse_ref
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.runs import continue_target_run, retention_run_ids


def test_retention_keeps_bound_and_reserved_continuation_sources() -> None:
    bound = SpawnRecord(id=parse_ref("p1", row_key=True), session_instance_id="generation")
    target = SpawnRecord(
        id=parse_ref("p1.2", row_key=True), chat_id="c1", owner_chat_id="c2",
    )
    latest = SpawnRecord(id=parse_ref("p1.3", row_key=True))
    rows = (bound, target, latest)

    assert continue_target_run(rows) == bound
    assert retention_run_ids(rows) == {bound.id, latest.id}
