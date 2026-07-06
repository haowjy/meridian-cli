"""Depth resolution from spawn ancestry records."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.depth import (
    depth_from_spawn_ancestry,
    resolve_effective_meridian_depth,
)
from meridian.lib.state.spawn.model import SpawnRecord


def _record(
    spawn_id: str,
    *,
    parent_id: str | None = None,
) -> SpawnRecord:
    return SpawnRecord(
        id=spawn_id,
        chat_id="chat-1",
        parent_id=parent_id,
        model="gpt-5.4",
        agent="coder",
        agent_path=None,
        skills=(),
        skill_paths=(),
        harness="codex",
        kind="child",
        desc="test spawn",
        work_id=None,
        harness_session_id=None,
        launch_mode="foreground",
        worker_pid=None,
        runner_pid=None,
        runner_created_at_epoch=None,
        status="running",
        prompt="hello",
        started_at=None,
        last_attempt_exited_at=None,
        last_attempt_exit_code=None,
        runner_exit_code=None,
        runner_exit_status=None,
        runner_exit_error=None,
        runner_exit_at=None,
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
    )


def test_depth_from_spawn_ancestry_counts_parents(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    records = {
        "p-root": _record("p-root"),
        "p-child": _record("p-child", parent_id="p-root"),
        "p-grandchild": _record("p-grandchild", parent_id="p-child"),
    }

    def _fake_get_spawn(_runtime_root: Path, spawn_id: str) -> SpawnRecord | None:
        return records.get(spawn_id)

    monkeypatch.setattr(
        "meridian.lib.state.spawn_store.get_spawn",
        _fake_get_spawn,
    )

    assert depth_from_spawn_ancestry("p-root", runtime_root) == 0
    assert depth_from_spawn_ancestry("p-child", runtime_root) == 1
    assert depth_from_spawn_ancestry("p-grandchild", runtime_root) == 2


def test_resolve_effective_meridian_depth_uses_max_of_env_and_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(
        "meridian.lib.state.spawn_store.get_spawn",
        lambda _runtime_root, spawn_id: _record(spawn_id, parent_id="p-root")
        if spawn_id == "p-child"
        else _record(spawn_id),
    )

    env = {
        "MERIDIAN_SPAWN_ID": "p-child",
        "MERIDIAN_DEPTH": "0",
    }
    assert resolve_effective_meridian_depth(env, runtime_root=runtime_root) == 1

    env["MERIDIAN_DEPTH"] = "2"
    assert resolve_effective_meridian_depth(env, runtime_root=runtime_root) == 2
