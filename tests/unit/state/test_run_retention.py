"""Retention keeps bound/latest runs, not failed reservation targets."""

from meridian.lib.core.refs import parse_ref
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.runs import retention_run_ids


def test_retention_keeps_bound_and_latest_not_reserved_target() -> None:
    bound = SpawnRecord(id=parse_ref("p1", row_key=True), session_instance_id="generation")
    target = SpawnRecord(
        id=parse_ref("p1.2", row_key=True), chat_id="c1", owner_chat_id="c2",
    )
    latest = SpawnRecord(id=parse_ref("p1.3", row_key=True))
    rows = (bound, target, latest)

    assert retention_run_ids(rows) == {bound.id, latest.id}
