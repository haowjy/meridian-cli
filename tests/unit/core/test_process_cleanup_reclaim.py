# qa-validated: reaper-escape-fix-test-cleanup
"""Focused tests for managed-primary scope cleanup sequencing."""

from __future__ import annotations

import time
from pathlib import Path

from meridian.lib.core.process_cleanup import cancel_managed_primary
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state.spawn.model import SpawnRecord


def _record(spawn_id: str = "spawn-1") -> SpawnRecord:
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
        worker_pid=321,
        runner_pid=111,
        runner_created_at_epoch=None,
        status="running",
        prompt="hello",
        started_at="2026-05-01T00:00:00Z",
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
        process_scopes=None,
    )


def _scope(
    scope_id: str,
    *,
    owner_policy: str = "spawn_owned",
    owner_id: str = "spawn-1",
    pid: int = 100,
) -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy=owner_policy,
        owner_id=owner_id,
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=pid,
        root_created_at_epoch=10.0,
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
    """Scope-record fallback should signal launcher first, then runtime scopes."""
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
        lambda root, spawn_id, release_id: release_id == scopes[2].release_id,
    )
    monkeypatch.setattr(
        process_cleanup,
        "mark_scope_released",
        lambda root, spawn_id, release_id: released.append(release_id),
    )
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    def _terminate_scope_sync(scope, *, grace_seconds: float, reason: str):
        terminate_calls.append(scope.scope_id)
        return _cleanup_result(scope.scope_id, scope.root_pid, reason)

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _terminate_scope_sync)

    results = cancel_managed_primary(tmp_path, _record(), grace_seconds=7.0)

    assert terminate_calls == ["launcher", "backend"]
    assert sleep_calls == [1.0]
    assert released == [scopes[1].release_id, scopes[0].release_id]
    assert [(result.scope_id, result.skip_reason) for result in results] == [
        ("launcher", None),
        ("backend", None),
        ("tui", "already_released"),
    ]
