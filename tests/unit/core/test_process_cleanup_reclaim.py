# qa-validated: test-suite-redesign
"""Tests for cancel_managed_primary and reclaim_session_owned_scopes_for_chat."""

from __future__ import annotations

import time
from pathlib import Path

from meridian.lib.core.process_cleanup import (
    cancel_managed_primary,
    reclaim_session_owned_scopes_for_chat,
)
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state.spawn.model import SpawnRecord


def _record(
    spawn_id: str = "spawn-1",
    *,
    chat_id: str = "chat-1",
    worker_pid: int | None = 321,
) -> SpawnRecord:
    return SpawnRecord(
        id=spawn_id,
        chat_id=chat_id,
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

    def _terminate_scope_sync(scope, *, grace_seconds: float, reason: str):
        terminate_calls.append(scope.scope_id)
        return _cleanup_result(scope.scope_id, scope.root_pid, reason)

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _terminate_scope_sync)

    results = cancel_managed_primary(tmp_path, _record(), grace_seconds=7.0)

    assert terminate_calls == ["launcher", "backend"]
    assert sleep_calls == [1.0]
    assert released == ["launcher", "backend"]
    assert [(result.scope_id, result.skip_reason) for result in results] == [
        ("launcher", None),
        ("backend", None),
        ("tui", "already_released"),
    ]


def test_reclaim_session_owned_scopes_for_chat_only_reclaims_matching_unreleased_scopes(
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

    def _terminate_scope_sync(scope, *, grace_seconds: float, reason: str):
        terminate_calls.append((scope.scope_id, scope.root_pid))
        return _cleanup_result(scope.scope_id, scope.root_pid, reason)

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _terminate_scope_sync)

    # Patch the deferred import so we control which spawns are returned.
    monkeypatch.setattr(
        "meridian.lib.state.spawn_store.list_spawns",
        lambda root, filters=None: [
            _record("spawn-1", chat_id="chat-x"),
            _record("spawn-2", chat_id="chat-x"),
        ],
    )

    results = reclaim_session_owned_scopes_for_chat(tmp_path, "chat-x", grace_seconds=3.0)

    # Only session_owned scopes that are not already released should be reclaimed.
    # spawn-1: backend (session_owned, unreleased) → reclaimed
    # spawn-1: other-session (session_owned, unreleased) → reclaimed
    # spawn-1: spawn-owned → skipped (spawn_owned policy)
    # spawn-2: released (is_scope_released returns True for spawn-2) → skipped
    assert set(result.scope_id for result in results) == {"backend", "other-session"}
    assert set(terminate_calls) == {("backend", 101), ("other-session", 102)}
    assert set(released) == {("spawn-1", "backend"), ("spawn-1", "other-session")}


def test_reclaim_session_owned_scopes_for_chat_skips_spawn_owned(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    scopes = [
        _scope("backend", owner_policy="session_owned", owner_id="session-a", pid=101),
        _scope("worker", owner_policy="spawn_owned", owner_id="spawn-1", pid=102),
    ]
    terminate_calls: list[str] = []
    released: list[str] = []

    monkeypatch.setattr(process_cleanup, "read_scopes_from_disk", lambda root, spawn_id: scopes)
    monkeypatch.setattr(
        process_cleanup, "is_scope_released", lambda root, spawn_id, scope_id: False
    )
    monkeypatch.setattr(
        process_cleanup,
        "mark_scope_released",
        lambda root, spawn_id, scope_id: released.append(scope_id),
    )

    def _terminate_scope_sync(scope, *, grace_seconds: float, reason: str):
        terminate_calls.append(scope.scope_id)
        return _cleanup_result(scope.scope_id, scope.root_pid, reason)

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _terminate_scope_sync)
    monkeypatch.setattr(
        "meridian.lib.state.spawn_store.list_spawns",
        lambda root, filters=None: [_record("spawn-1", chat_id="chat-y")],
    )

    results = reclaim_session_owned_scopes_for_chat(tmp_path, "chat-y")

    assert [r.scope_id for r in results] == ["backend"]
    assert terminate_calls == ["backend"]
    assert released == ["backend"]


def test_reclaim_session_owned_scopes_for_chat_skips_already_released(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    scopes = [
        _scope("backend", owner_policy="session_owned", owner_id="session-a", pid=101),
    ]
    terminate_calls: list[str] = []

    monkeypatch.setattr(process_cleanup, "read_scopes_from_disk", lambda root, spawn_id: scopes)
    monkeypatch.setattr(
        process_cleanup, "is_scope_released", lambda root, spawn_id, scope_id: True
    )
    monkeypatch.setattr(
        process_cleanup, "mark_scope_released", lambda *_: None
    )

    def _unexpected(scope, **kwargs):
        raise AssertionError("terminate_scope_sync must not be called for released scopes")

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _unexpected)
    monkeypatch.setattr(
        "meridian.lib.state.spawn_store.list_spawns",
        lambda root, filters=None: [_record("spawn-1", chat_id="chat-z")],
    )

    results = reclaim_session_owned_scopes_for_chat(tmp_path, "chat-z")

    assert results == []
    assert terminate_calls == []


def test_reclaim_session_owned_scopes_for_chat_multiple_spawns(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    scopes_s1 = [_scope("backend", owner_policy="session_owned", owner_id="s1", pid=101)]
    scopes_s2 = [_scope("tui", owner_policy="session_owned", owner_id="s2", pid=201)]
    scopes_by_spawn = {"spawn-1": scopes_s1, "spawn-2": scopes_s2}
    terminate_calls: list[str] = []
    released: list[str] = []

    monkeypatch.setattr(
        process_cleanup,
        "read_scopes_from_disk",
        lambda root, spawn_id: scopes_by_spawn[str(spawn_id)],
    )
    monkeypatch.setattr(
        process_cleanup, "is_scope_released", lambda root, spawn_id, scope_id: False
    )
    monkeypatch.setattr(
        process_cleanup,
        "mark_scope_released",
        lambda root, spawn_id, scope_id: released.append(scope_id),
    )

    def _terminate_scope_sync(scope, *, grace_seconds: float, reason: str):
        terminate_calls.append(scope.scope_id)
        return _cleanup_result(scope.scope_id, scope.root_pid, reason)

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _terminate_scope_sync)
    monkeypatch.setattr(
        "meridian.lib.state.spawn_store.list_spawns",
        lambda root, filters=None: [
            _record("spawn-1", chat_id="chat-multi"),
            _record("spawn-2", chat_id="chat-multi"),
        ],
    )

    results = reclaim_session_owned_scopes_for_chat(tmp_path, "chat-multi")

    assert {r.scope_id for r in results} == {"backend", "tui"}
    assert set(terminate_calls) == {"backend", "tui"}
    assert set(released) == {"backend", "tui"}
