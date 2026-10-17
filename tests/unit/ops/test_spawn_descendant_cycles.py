"""Regression coverage for malformed spawn parent-link cycles."""

from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_tree import collect_descendants


def _cycle(*, child_status: str) -> list[SpawnRecord]:
    return [
        SpawnRecord(id="p1", parent_id="p2", status="succeeded"),
        SpawnRecord(id="p2", parent_id="p1", status=child_status),
    ]


def test_collect_descendants_deduplicates_a_cycle() -> None:
    descendants = collect_descendants("p1", _cycle(child_status="running"))

    assert [row.id for row in descendants] == ["p1", "p2"]
