# qa-validated: reaper-escape-fix-test-cleanup
"""Focused tests for spawn-scope cleanup and session-lease preservation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import psutil as _psutil
import pytest

from meridian.lib.core.process_cleanup import should_skip_cleanup, terminate_spawn_scopes
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


def test_terminate_spawn_scopes_uses_persisted_scopes_and_marks_released(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    scopes = [_scope("backend", pid=101), _scope("worker", pid=202)]
    released: list[tuple[str, str]] = []
    terminate_calls: list[tuple[int, float, str, str]] = []

    monkeypatch.setattr(process_cleanup, "read_scopes_from_disk", lambda root, spawn_id: scopes)
    monkeypatch.setattr(
        process_cleanup, "is_scope_released", lambda root, spawn_id, scope_id: False
    )
    monkeypatch.setattr(
        process_cleanup,
        "mark_scope_released",
        lambda root, spawn_id, scope_id: released.append((str(spawn_id), scope_id)),
    )

    def _terminate_scope_sync(scope, *, grace_seconds: float, reason: str):
        terminate_calls.append(
            (scope.root_pid, scope.root_created_at_epoch, reason, scope.scope_id)
        )
        return _cleanup_result(scope.scope_id, scope.root_pid, reason)

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _terminate_scope_sync)

    results = terminate_spawn_scopes(tmp_path, _record(), reason="reaper", grace_seconds=4.0)

    assert [result.scope_id for result in results] == ["backend", "worker"]
    assert terminate_calls == [
        (101, 10.0, "reaper", "backend"),
        (202, 10.0, "reaper", "worker"),
    ]
    assert released == [
        ("spawn-1", scopes[0].release_id),
        ("spawn-1", scopes[1].release_id),
    ]


def test_terminate_spawn_scopes_skips_released_and_active_session_leases(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    scopes = [
        _scope("released", pid=101),
        _scope("leased", owner_policy="session_owned", owner_id="session-9", pid=202),
    ]

    monkeypatch.setattr(process_cleanup, "read_scopes_from_disk", lambda root, spawn_id: scopes)
    monkeypatch.setattr(
        process_cleanup,
        "is_scope_released",
        lambda root, spawn_id, release_id: release_id == scopes[0].release_id,
    )
    monkeypatch.setattr(
        process_cleanup.psutil,
        "Process",
        lambda pid: type("LiveProc", (), {"create_time": lambda self: 10.0})(),
    )

    def _unexpected(*args, **kwargs):
        raise AssertionError("terminate_scope_sync should not be called for skipped scopes")

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _unexpected)

    results = terminate_spawn_scopes(tmp_path, _record(), reason="reaper", grace_seconds=4.0)

    assert [(result.scope_id, result.skip_reason) for result in results] == [
        ("released", "already_released"),
        ("leased", "active_session_lease"),
    ]


def test_terminate_spawn_scopes_falls_back_to_legacy_worker_pid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    legacy_result = CleanupResult(
        scope_id="legacy_worker",
        root_pid=321,
        descendant_count=0,
        reason="reaper",
        grace_seconds=5.0,
        kill_escalated=False,
        degraded_fallback=False,
        skip_reason=None,
    )

    monkeypatch.setattr(process_cleanup, "read_scopes_from_disk", lambda root, spawn_id: [])
    monkeypatch.setattr(process_cleanup, "terminate_tree_sync", lambda **kwargs: legacy_result)
    logger = MagicMock()
    monkeypatch.setattr(process_cleanup, "logger", logger)

    results = terminate_spawn_scopes(tmp_path, _record(worker_pid=321), reason="reaper")

    assert results == [legacy_result]
    logger.warning.assert_not_called()
    logger.debug.assert_called_once()


def test_terminate_spawn_scopes_can_skip_legacy_worker_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    monkeypatch.setattr(process_cleanup, "read_scopes_from_disk", lambda root, spawn_id: [])

    def _unexpected(**kwargs):
        raise AssertionError("legacy worker fallback should not run")

    monkeypatch.setattr(process_cleanup, "terminate_tree_sync", _unexpected)

    results = terminate_spawn_scopes(
        tmp_path,
        _record(worker_pid=321),
        reason="cancel",
        include_legacy_fallback=False,
    )

    assert results == []


@pytest.mark.parametrize(
    ("process_factory", "root_created_at_epoch", "expected"),
    [
        (lambda: (_ for _ in ()).throw(_psutil.NoSuchProcess(pid=12345)), 10.0, False),
        (lambda: MagicMock(create_time=MagicMock(return_value=99_999.0)), 1_000_000.0, False),
        (lambda: MagicMock(create_time=MagicMock(return_value=1_000_000.1)), 1_000_000.0, True),
    ],
)
def test_should_skip_cleanup_validates_session_owned_process_liveness(
    monkeypatch,
    process_factory,
    root_created_at_epoch: float,
    expected: bool,
) -> None:
    """Session-owned scopes are preserved only while the validated root process is alive."""
    from meridian.lib.core import process_cleanup

    scope = _scope(
        "backend",
        owner_policy="session_owned",
        owner_id="session-abc",
        pid=12345,
        root_created_at_epoch=root_created_at_epoch,
    )
    record = _record()

    def _process(_pid: int):
        return process_factory()

    monkeypatch.setattr(process_cleanup.psutil, "Process", _process)

    assert should_skip_cleanup(scope, record) is expected


def test_should_skip_cleanup_spawn_owned_never_skips() -> None:
    """Spawn-owned scopes should always be eligible for cleanup."""
    scope = _scope("worker", owner_policy="spawn_owned", owner_id="spawn-1", pid=55555)

    assert should_skip_cleanup(scope, _record()) is False
