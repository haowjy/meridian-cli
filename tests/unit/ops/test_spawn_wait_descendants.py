"""Test that no-arg spawn wait scopes to descendants when nested."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from meridian.lib.ops.spawn.api import _discover_pending_spawns
from meridian.lib.state.spawn.model import SpawnRecord


def _make_record(
    spawn_id: str,
    *,
    parent_id: str | None = None,
    chat_id: str = "c1",
    status: str = "running",
) -> SpawnRecord:
    return SpawnRecord(
        id=spawn_id,
        parent_id=parent_id,
        chat_id=chat_id,
        status=status,
        model="test",
        agent="test",
        agent_path=None,
        skills=(),
        skill_paths=(),
        harness="test",
        harness_id="test",
        kind="child",
        desc="test",
        work_id=None,
        goal=None,
        harness_session_id=None,
        execution_cwd=None,
        claude_config_dir=None,
        launch_mode="background",
        worker_pid=None,
        runner_pid=None,
        prompt="test",
        started_at="2026-01-01T00:00:00Z",
        exited_at=None,
        process_exit_code=None,
        finished_at=None,
        exit_code=None,
        duration_secs=None,
        total_cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
        reasoning_tokens=None,
        cost_is_estimate=False,
        error=None,
        terminal_origin=None,
        process_scopes=None,
    )


def _patch_spawns(spawns: list[SpawnRecord]):
    """Patch list_spawns to return the given records."""
    return patch("meridian.lib.ops.spawn.api.spawn_store.list_spawns", return_value=spawns)


def test_no_descendant_filter_returns_all_active(tmp_path: Path) -> None:
    """Without only_descendants_of, all active spawns in the chat are returned."""
    spawns = [
        _make_record("p1", parent_id=None),
        _make_record("p2", parent_id="p1"),
        _make_record("p3", parent_id="p1"),
    ]
    with _patch_spawns(spawns):
        result = _discover_pending_spawns(tmp_path, tmp_path, "c1", exclude_spawn_id="p2")
    assert {r.id for r in result} == {"p1", "p3"}


def test_descendant_filter_excludes_ancestors_and_siblings(tmp_path: Path) -> None:
    """With only_descendants_of, only children/grandchildren are returned."""
    spawns = [
        _make_record("p1", parent_id=None),  # primary — ancestor
        _make_record("p2", parent_id="p1"),  # tech-lead (self)
        _make_record("p3", parent_id="p2"),  # child of p2
        _make_record("p4", parent_id="p2"),  # child of p2
        _make_record("p5", parent_id="p1"),  # sibling
        _make_record("p6", parent_id="p3"),  # grandchild of p2
    ]
    with _patch_spawns(spawns):
        result = _discover_pending_spawns(
            tmp_path,
            tmp_path,
            "c1",
            exclude_spawn_id="p2",
            only_descendants_of="p2",
        )
    # Should only get p3, p4, p6 (descendants of p2)
    # NOT p1 (ancestor), p2 (self, excluded), or p5 (sibling)
    assert {r.id for r in result} == {"p3", "p4", "p6"}


def test_descendant_filter_with_completed_children(tmp_path: Path) -> None:
    """Completed descendants are excluded (only active ones returned)."""
    spawns = [
        _make_record("p1", parent_id=None),
        _make_record("p2", parent_id="p1"),
        _make_record("p3", parent_id="p2", status="succeeded"),
        _make_record("p4", parent_id="p2", status="running"),
    ]
    with _patch_spawns(spawns):
        result = _discover_pending_spawns(
            tmp_path,
            tmp_path,
            "c1",
            exclude_spawn_id="p2",
            only_descendants_of="p2",
        )
    assert {r.id for r in result} == {"p4"}


def test_descendant_filter_no_children_returns_empty(tmp_path: Path) -> None:
    """When the spawn has no descendants, returns empty."""
    spawns = [
        _make_record("p1", parent_id=None),
        _make_record("p2", parent_id="p1"),
        _make_record("p5", parent_id="p1"),
    ]
    with _patch_spawns(spawns):
        result = _discover_pending_spawns(
            tmp_path,
            tmp_path,
            "c1",
            exclude_spawn_id="p2",
            only_descendants_of="p2",
        )
    assert result == []


def test_discovery_does_not_reconcile_unrelated_spawns(tmp_path: Path) -> None:
    """No-arg wait discovery must not run global stale-spawn cleanup."""
    spawns = [
        _make_record("p1", parent_id=None),
        _make_record("p2", parent_id="p1"),
    ]
    with (
        _patch_spawns(spawns),
        patch("meridian.lib.state.reaper.reconcile_spawns") as reconcile_spawns,
    ):
        result = _discover_pending_spawns(tmp_path, tmp_path, "c1", exclude_spawn_id="p1")

    assert [r.id for r in result] == ["p2"]
    reconcile_spawns.assert_not_called()
