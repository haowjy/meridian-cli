# qa-validated: reaper-escape-fix-test-cleanup
"""Integration coverage for persisted spawn-scope cleanup."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psutil as _psutil
import pytest

from meridian.lib.core.process_cleanup import (
    terminate_recorded_spawn_scopes,
    terminate_spawn_scopes,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state import spawn_store
from meridian.lib.state.process_scope_projection import (
    is_scope_released,
    mark_scope_released,
    record_scope,
)
from meridian.lib.state.spawn.model import SpawnRecord


def _persist_spawn(
    runtime_root: Path,
    *,
    spawn_id: str = "spawn-1",
    worker_pid: int | None = 321,
) -> SpawnRecord:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        kind="child",
        prompt="hello",
        worker_pid=worker_pid,
        runner_pid=111,
        started_at="2026-05-01T00:00:00Z",
        status="running",
    )
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert record is not None
    return record


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


def _cleanup_result(
    scope_id: str,
    root_pid: int,
    reason: str,
    grace_seconds: float,
) -> CleanupResult:
    return CleanupResult(
        scope_id=scope_id,
        root_pid=root_pid,
        descendant_count=2,
        reason=reason,
        grace_seconds=grace_seconds,
        kill_escalated=False,
        degraded_fallback=False,
        skip_reason=None,
    )


def test_terminate_spawn_scopes_reads_persisted_scopes_and_marks_released(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    record = _persist_spawn(tmp_path)
    scopes = [_scope("backend", pid=101), _scope("worker", pid=202)]
    for scope in scopes:
        record_scope(tmp_path, SpawnId(record.id), scope)
    terminate_calls: list[tuple[int, float, str, str]] = []

    def _terminate_scope_sync(
        scope: ProcessScopeSnapshot,
        *,
        grace_seconds: float,
        reason: str,
    ) -> CleanupResult:
        terminate_calls.append(
            (scope.root_pid, scope.root_created_at_epoch, reason, scope.scope_id)
        )
        return _cleanup_result(scope.scope_id, scope.root_pid, reason, grace_seconds)

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _terminate_scope_sync)

    results = terminate_spawn_scopes(tmp_path, record, reason="reaper", grace_seconds=4.0)

    assert [result.scope_id for result in results] == ["backend", "worker"]
    assert terminate_calls == [
        (101, 10.0, "reaper", "backend"),
        (202, 10.0, "reaper", "worker"),
    ]
    assert all(
        is_scope_released(tmp_path, SpawnId(record.id), scope.release_id)
        for scope in scopes
    )


def test_terminate_spawn_scopes_skips_persisted_release_and_active_session_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    record = _persist_spawn(tmp_path)
    released = _scope("released", pid=101)
    leased = _scope(
        "leased",
        owner_policy="session_owned",
        owner_id="session-9",
        pid=202,
    )
    for scope in (released, leased):
        record_scope(tmp_path, SpawnId(record.id), scope)
    mark_scope_released(tmp_path, SpawnId(record.id), released.release_id)
    monkeypatch.setattr(
        process_cleanup.psutil,
        "Process",
        lambda pid: type("LiveProc", (), {"create_time": lambda self: 10.0})(),
    )

    def _unexpected(*args: object, **kwargs: object) -> CleanupResult:
        raise AssertionError("terminate_scope_sync should not be called for skipped scopes")

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _unexpected)

    results = terminate_spawn_scopes(tmp_path, record, reason="reaper", grace_seconds=4.0)

    assert [(result.scope_id, result.skip_reason) for result in results] == [
        ("released", "already_released"),
        ("leased", "active_session_lease"),
    ]
    assert not is_scope_released(tmp_path, SpawnId(record.id), leased.release_id)


def test_terminate_spawn_scopes_falls_back_to_legacy_worker_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    record = _persist_spawn(tmp_path, worker_pid=321)
    legacy_result = _cleanup_result("legacy_worker", 321, "reaper", 5.0)
    monkeypatch.setattr(process_cleanup, "terminate_tree_sync", lambda **kwargs: legacy_result)

    results = terminate_spawn_scopes(tmp_path, record, reason="reaper")

    assert results == [legacy_result]


def test_terminate_recorded_spawn_scopes_does_not_run_legacy_worker_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    record = _persist_spawn(tmp_path, worker_pid=321)

    def _unexpected(**kwargs: object) -> CleanupResult:
        raise AssertionError("legacy worker fallback should not run")

    monkeypatch.setattr(process_cleanup, "terminate_tree_sync", _unexpected)

    results = terminate_recorded_spawn_scopes(tmp_path, record, reason="cancel")

    assert results == []


@pytest.mark.parametrize(
    ("process_factory", "root_created_at_epoch", "expected_skip"),
    [
        (lambda: (_ for _ in ()).throw(_psutil.NoSuchProcess(pid=12345)), 10.0, False),
        (
            lambda: type("ReusedProc", (), {"create_time": lambda self: 99_999.0})(),
            1_000_000.0,
            False,
        ),
        (
            lambda: type(
                "LiveProc", (), {"create_time": lambda self: 1_000_000.1}
            )(),
            1_000_000.0,
            True,
        ),
    ],
    ids=["dead", "birth-mismatch", "birth-match"],
)
def test_session_lease_cleanup_uses_birth_checked_os_liveness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    process_factory: Callable[[], object],
    root_created_at_epoch: float,
    expected_skip: bool,
) -> None:
    """Only a live, birth-matched session lease survives persisted cleanup."""
    from meridian.lib.core import process_cleanup

    record = _persist_spawn(tmp_path)
    scope = _scope(
        "backend",
        owner_policy="session_owned",
        owner_id="session-abc",
        pid=12345,
        root_created_at_epoch=root_created_at_epoch,
    )
    record_scope(tmp_path, SpawnId(record.id), scope)
    monkeypatch.setattr(process_cleanup.psutil, "Process", lambda _pid: process_factory())
    terminated: list[str] = []

    def _terminate_scope_sync(
        candidate: ProcessScopeSnapshot,
        *,
        grace_seconds: float,
        reason: str,
    ) -> CleanupResult:
        terminated.append(candidate.release_id)
        return _cleanup_result(
            candidate.scope_id, candidate.root_pid, reason, grace_seconds
        )

    monkeypatch.setattr(process_cleanup, "terminate_scope_sync", _terminate_scope_sync)

    [result] = terminate_spawn_scopes(tmp_path, record, reason="reaper")

    assert (result.skip_reason == "active_session_lease") is expected_skip
    assert terminated == ([] if expected_skip else [scope.release_id])
    assert is_scope_released(tmp_path, SpawnId(record.id), scope.release_id) is (
        not expected_skip
    )
