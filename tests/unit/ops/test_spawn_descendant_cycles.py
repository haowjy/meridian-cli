"""Pure regression coverage for malformed spawn parent-link cycles."""

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.refs import parse_ref, spawn_of
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_tree import collect_descendants, has_outstanding_descendant_work


def _row(
    run_id: str,
    *,
    parent_id: str | None = None,
    status: SpawnStatus,
) -> SpawnRecord:
    return SpawnRecord(
        id=parse_ref(run_id, row_key=True),
        parent_id=spawn_of(parent_id) if parent_id is not None else None,
        status=status,
    )


def _cycle(*, child_status: SpawnStatus) -> list[SpawnRecord]:
    return [
        _row("p1", parent_id="p2", status=SpawnStatus.SUCCEEDED),
        _row("p2", parent_id="p1", status=child_status),
    ]


def test_outstanding_descendant_work_is_cycle_safe() -> None:
    assert (
        has_outstanding_descendant_work("p1", _cycle(child_status=SpawnStatus.SUCCEEDED)) is False
    )
    assert has_outstanding_descendant_work("p1", _cycle(child_status=SpawnStatus.RUNNING)) is True


def test_collect_descendants_deduplicates_a_cycle() -> None:
    descendants = collect_descendants("p1", _cycle(child_status=SpawnStatus.RUNNING))

    assert [row.id for row in descendants] == ["p1", "p2"]


def test_collect_descendants_unions_run_parents_and_preserves_run_rows() -> None:
    rows = [
        _row("p1", status=SpawnStatus.SUCCEEDED),
        _row("p2", parent_id="p1", status=SpawnStatus.RUNNING),
        _row("p2.2", parent_id="p9", status=SpawnStatus.SUCCEEDED),
        _row("p3", parent_id="p2", status=SpawnStatus.RUNNING),
    ]

    descendants = collect_descendants("p1", rows)

    assert [row.id for row in descendants] == ["p1", "p2", "p2.2", "p3"]


def test_collect_descendants_keeps_noncanonical_rows_distinct() -> None:
    rows = [
        _row("p1", status=SpawnStatus.SUCCEEDED),
        _row("p2.0", parent_id="p1", status=SpawnStatus.RUNNING),
        _row("p2", parent_id="p1", status=SpawnStatus.RUNNING),
        _row("p2.2", parent_id="p9", status=SpawnStatus.SUCCEEDED),
    ]

    descendants = collect_descendants("p1", rows)

    assert [row.id for row in descendants] == ["p1", "p2.0", "p2", "p2.2"]
