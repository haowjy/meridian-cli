"""Regression coverage for malformed spawn parent-link cycles."""

from pathlib import Path

from meridian.lib.state import spawn_store
from meridian.lib.state.launch_lock import LaunchSlot
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_tree import collect_descendants, has_outstanding_descendant_work


def _cycle(*, child_status: str) -> list[SpawnRecord]:
    return [
        SpawnRecord(id="p1", parent_id="p2", status="succeeded"),
        SpawnRecord(id="p2", parent_id="p1", status=child_status),
    ]


def test_outstanding_descendant_work_is_derived_and_cycle_safe(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    assert (
        has_outstanding_descendant_work(
            runtime_root,
            "p1",
            _cycle(child_status="succeeded"),
        )
        is False
    )

    with LaunchSlot() as launch_lock:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p2",
            parent_id="p1",
            chat_id="c1",
            model="test",
            agent="coder",
            harness="codex",
            prompt="hello",
            launch_lock=launch_lock,
        )
        assert (
            has_outstanding_descendant_work(
                runtime_root,
                "p1",
                _cycle(child_status="running"),
            )
            is True
        )

    assert (
        has_outstanding_descendant_work(
            runtime_root,
            "p1",
            _cycle(child_status="running"),
        )
        is False
    )


def test_collect_descendants_deduplicates_a_cycle() -> None:
    descendants = collect_descendants("p1", _cycle(child_status="running"))

    assert [row.id for row in descendants] == ["p1", "p2"]
