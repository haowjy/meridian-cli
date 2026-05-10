# qa-validated: test-suite-redesign
"""Tests for cancel_managed_primary and reclaim_stale_session_scopes."""

from __future__ import annotations

import time
from pathlib import Path

from meridian.lib.core.process_cleanup import (
    cancel_managed_primary,
    reclaim_stale_session_scopes,
)
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state.spawn.model import SpawnRecord


def _record(
    spawn_id: str = "spawn-1",
    *,
    worker_pid: int | None = 321,
) -> SpawnRecord:
    return SpawnRecord(
        id=spawn_id,
        chat_id="chat-1",
        parent_id=None,
        model="gpt-5.4",
        agent="coder",
        agent_path=None,
        skills=(),
        skill_paths=(),
        harness="codex",
        kind="child",
        desc="test spawn",
        work_id="work-1",
        goal="goal",
        harness_session_id="session-1",
        execution_cwd="/tmp/project",
        claude_config_dir=None,
        launch_mode="background",
        worker_pid=worker_pid,
        runner_pid=111,
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
        process_scopes=None,
    )


def _scope(
    scope_id: str,
    *,
    owner_policy: str = "spawn_owned",
    owner_id: str = "spawn-1",
    pid: int = 100,
    root_created_at_epoch: float = 10.0,
) -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy=owner_policy,
        owner_id=owner_id,
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=pid,
        root_created_at_epoch=root_created_at_epoch,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )


def _cleanup_result(scope_id: str, root_pid: int, reason: str) -> CleanupResult:
    return CleanupResult(
        scope_id=scope_id,
        root_pid=root_pid,
        descendant_count=2,
        reason=reason,
        grace_seconds=5.0,
        kill_escalated=False,
        degraded_fallback=False,
        skip_reason=None,
    )


def test_cancel_managed_primary_terminates_launcher_before_runtime_scopes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    scopes = [
        _scope("backend", owner_policy="session_owned", owner_id="session-1", pid=202),
        _scope("launcher", pid=101),
        _scope("tui", owner_policy="session_owned", owner_id="session-1", pid=303),
    ]
    terminate_calls: list[str] = []
    released: list[str] = []
    sleep_calls: list[float] = []

    monkeypatch.setattr(process_cleanup, "read_scopes_from_disk", lambda root, spawn_id: scopes)
    monkeypatch.setattr(
        process_cleanup,
        "is_scope_released",
        lambda root, spawn_id, scope_id: scope_id == "tui",
    )
    monkeypatch.setattr(
        process_cleanup,
        "mark_scope_released",
        lambda root, spawn_id, scope_id: released.append(scope_id),
    )
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    def _terminate_tree_sync(
        *, pid: int, created_at_epoch: float, grace_secs: float, reason: str, scope_id: str
    ):
        terminate_calls.append(scope_id)
        return _cleanup_result(scope_id, pid, reason)

    monkeypatch.setattr(process_cleanup, "terminate_tree_sync", _terminate_tree_sync)

    results = cancel_managed_primary(tmp_path, _record(), grace_seconds=7.0)

    assert terminate_calls == ["launcher", "backend"]
    assert sleep_calls == [1.0]
    assert released == ["launcher", "backend"]
    assert [(result.scope_id, result.skip_reason) for result in results] == [
        ("launcher", None),
        ("backend", None),
        ("tui", "already_released"),
    ]


def test_reclaim_stale_session_scopes_only_reclaims_matching_unreleased_scopes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    spawn_one_scopes = [
        _scope("backend", owner_policy="session_owned", owner_id="session-a", pid=101),
        _scope("other-session", owner_policy="session_owned", owner_id="session-b", pid=102),
        _scope("spawn-owned", owner_policy="spawn_owned", owner_id="spawn-1", pid=103),
    ]
    spawn_two_scopes = [
        _scope("released", owner_policy="session_owned", owner_id="session-a", pid=201),
    ]
    scopes_by_spawn = {
        "spawn-1": spawn_one_scopes,
        "spawn-2": spawn_two_scopes,
    }
    released: list[tuple[str, str]] = []
    terminate_calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        process_cleanup,
        "read_scopes_from_disk",
        lambda root, spawn_id: scopes_by_spawn[str(spawn_id)],
    )
    monkeypatch.setattr(
        process_cleanup,
        "is_scope_released",
        lambda root, spawn_id, scope_id: str(spawn_id) == "spawn-2",
    )
    monkeypatch.setattr(
        process_cleanup,
        "mark_scope_released",
        lambda root, spawn_id, scope_id: released.append((str(spawn_id), scope_id)),
    )

    def _terminate_tree_sync(
        *, pid: int, created_at_epoch: float, grace_secs: float, reason: str, scope_id: str
    ):
        terminate_calls.append((scope_id, pid))
        return _cleanup_result(scope_id, pid, reason)

    monkeypatch.setattr(process_cleanup, "terminate_tree_sync", _terminate_tree_sync)

    results = reclaim_stale_session_scopes(
        tmp_path,
        "session-a",
        [_record("spawn-1"), _record("spawn-2")],
        grace_seconds=3.0,
    )

    assert [(result.scope_id, result.root_pid) for result in results] == [("backend", 101)]
    assert terminate_calls == [("backend", 101)]
    assert released == [("spawn-1", "backend")]
