from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from meridian.lib.state.process_scope_projection import (
    is_scope_released,
    mark_scope_released,
    read_scopes,
    read_scopes_from_disk,
    record_scope,
)
from meridian.lib.state.spawn.model import SpawnRecord


def _snapshot(scope_id: str, root_pid: int) -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy="spawn_owned",
        owner_id="p1",
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=root_pid,
        root_created_at_epoch=100.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )


def _record_with_process_scopes(
    process_scopes: tuple[dict[str, object], ...] | None,
) -> SpawnRecord:
    return SpawnRecord(
        id="p1",
        chat_id="c1",
        parent_id=None,
        model="gpt-5.4",
        agent="coder",
        agent_path=None,
        skills=(),
        skill_paths=(),
        harness="codex",
        kind="child",
        desc=None,
        work_id=None,
        goal=None,
        harness_session_id=None,
        execution_cwd=None,
        claude_config_dir=None,
        launch_mode="background",
        worker_pid=None,
        runner_pid=123,
        status="running",
        prompt="hello",
        started_at="2026-05-01T00:00:00Z",
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
        process_scopes=process_scopes,
    )


def test_record_scope_round_trips_from_disk(tmp_path: Path) -> None:
    runtime_root = tmp_path
    first = _snapshot("backend", 111)
    second = _snapshot("worker", 222)

    record_scope(runtime_root, SpawnId("p1"), first)
    record_scope(runtime_root, SpawnId("p1"), second)

    assert read_scopes_from_disk(runtime_root, SpawnId("p1")) == [first, second]


def test_mark_scope_released_is_idempotent_and_durable(tmp_path: Path) -> None:
    runtime_root = tmp_path
    record_scope(runtime_root, SpawnId("p1"), _snapshot("backend", 111))

    mark_scope_released(runtime_root, SpawnId("p1"), "backend")
    mark_scope_released(runtime_root, SpawnId("p1"), "backend")

    assert is_scope_released(runtime_root, SpawnId("p1"), "backend") is True
    payload = json.loads(
        (runtime_root / "spawns" / "p1" / "process_scopes.json").read_text(encoding="utf-8")
    )
    assert payload["released"] == ["backend"]


def test_read_scopes_tolerates_legacy_and_corrupt_entries() -> None:
    valid = _snapshot("backend", 111)
    legacy = _record_with_process_scopes(None)
    mixed = _record_with_process_scopes(
        (
            valid.__dict__,
            {"scope_id": "broken"},
        )
    )

    assert read_scopes(legacy) == []
    assert read_scopes(mixed) == [valid]
