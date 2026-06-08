"""Regression coverage for malformed spawn parent-link cycles."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.ops.spawn.api import collect_descendants
from meridian.lib.ops.spawn.outstanding import has_outstanding_descendant_work
from meridian.lib.state import spawn_store


def _start_row(
    runtime_root: Path,
    spawn_id: str,
    *,
    parent_id: str | None,
    status: str = "running",
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=SpawnId(spawn_id),
        chat_id=spawn_id,
        parent_id=parent_id,
        model="test-model",
        agent="test-agent",
        harness=HarnessId.CODEX.value,
        prompt="hello",
        status=status,
    )


def test_outstanding_descendant_work_returns_false_for_terminal_cycle(
    tmp_path: Path,
) -> None:
    _start_row(tmp_path, "p1", parent_id="p2")
    _start_row(tmp_path, "p2", parent_id="p1")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p1"), "succeeded", 0, origin="runner")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p2"), "succeeded", 0, origin="runner")

    assert has_outstanding_descendant_work(tmp_path, "p1") is False


def test_outstanding_descendant_work_finds_live_node_in_cycle(tmp_path: Path) -> None:
    _start_row(tmp_path, "p1", parent_id="p2")
    _start_row(tmp_path, "p2", parent_id="p1")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p1"), "succeeded", 0, origin="runner")

    assert has_outstanding_descendant_work(tmp_path, "p1") is True


def test_collect_descendants_dedupes_malformed_cycle(tmp_path: Path) -> None:
    _start_row(tmp_path, "p1", parent_id="p2")
    _start_row(tmp_path, "p2", parent_id="p1")

    descendants = collect_descendants("p1", spawn_store.list_spawns(tmp_path))

    assert [row.id for row in descendants] == ["p1", "p2"]
