"""Pure regression coverage for malformed spawn parent-link cycles."""

from uuid import NAMESPACE_URL, uuid5

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.refs import parse_ref, spawn_of
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_tree import (
    collect_run_parent_subtree,
    has_outstanding_descendant_work,
)


def _row(
    run_id: str,
    *,
    parent_id: str | None = None,
    exact_parent_run: str | None = None,
    status: SpawnStatus,
) -> SpawnRecord:
    return SpawnRecord(
        id=parse_ref(run_id, row_key=True),
        history_id=uuid5(NAMESPACE_URL, run_id),
        parent_id=spawn_of(parent_id) if parent_id is not None else None,
        parent_history_id=(
            uuid5(NAMESPACE_URL, exact_parent_run) if exact_parent_run is not None else None
        ),
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
    descendants = collect_run_parent_subtree(
        "p1", _cycle(child_status=SpawnStatus.RUNNING)
    )

    assert [row.id for row in descendants] == ["p1", "p2"]


def test_collect_descendants_requires_each_runs_own_parent_edge() -> None:
    rows = [
        _row("p1", status=SpawnStatus.SUCCEEDED),
        _row("p2", parent_id="p1", status=SpawnStatus.RUNNING),
        _row("p2.2", parent_id="p9", status=SpawnStatus.SUCCEEDED),
        _row("p3", parent_id="p2", status=SpawnStatus.RUNNING),
    ]

    descendants = collect_run_parent_subtree("p1", rows)

    assert [row.id for row in descendants] == ["p1", "p2", "p3"]


def test_collect_descendants_keeps_noncanonical_rows_distinct() -> None:
    rows = [
        _row("p1", status=SpawnStatus.SUCCEEDED),
        _row("p2.0", parent_id="p1", status=SpawnStatus.RUNNING),
        _row("p2", parent_id="p1", status=SpawnStatus.RUNNING),
        _row("p2.2", parent_id="p9", status=SpawnStatus.SUCCEEDED),
    ]

    descendants = collect_run_parent_subtree("p1", rows)

    assert [row.id for row in descendants] == ["p1", "p2.0", "p2"]


def test_exact_parent_run_keeps_reopened_grandchild_in_its_new_subtree() -> None:
    rows = [
        _row("p1", status=SpawnStatus.SUCCEEDED),
        _row(
            "p2",
            parent_id="p1",
            exact_parent_run="p1",
            status=SpawnStatus.SUCCEEDED,
        ),
        _row("p9", status=SpawnStatus.RUNNING),
        _row(
            "p2.2",
            parent_id="p9",
            exact_parent_run="p9",
            status=SpawnStatus.RUNNING,
        ),
        _row(
            "p10",
            parent_id="p2",
            exact_parent_run="p2.2",
            status=SpawnStatus.RUNNING,
        ),
    ]

    assert [row.id for row in collect_run_parent_subtree("p1", rows)] == ["p1", "p2"]
    assert [row.id for row in collect_run_parent_subtree("p9", rows)] == [
        "p9",
        "p2.2",
        "p10",
    ]
    assert not has_outstanding_descendant_work("p1", rows)
    assert has_outstanding_descendant_work("p9", rows)
