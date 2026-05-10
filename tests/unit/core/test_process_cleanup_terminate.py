# qa-validated: test-suite-redesign
"""Tests for terminate_spawn_scopes, should_skip_cleanup, and log emission."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import psutil as _psutil

from meridian.lib.core.process_cleanup import (
    should_skip_cleanup,
    terminate_spawn_scopes,
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

    def _terminate_tree_sync(
        *, pid: int, created_at_epoch: float, grace_secs: float, reason: str, scope_id: str
    ):
        terminate_calls.append((pid, created_at_epoch, reason, scope_id))
        return _cleanup_result(scope_id, pid, reason)

    monkeypatch.setattr(process_cleanup, "terminate_tree_sync", _terminate_tree_sync)

    results = terminate_spawn_scopes(tmp_path, _record(), reason="reaper", grace_seconds=4.0)

    assert [result.scope_id for result in results] == ["backend", "worker"]
    assert terminate_calls == [
        (101, 10.0, "reaper", "backend"),
        (202, 10.0, "reaper", "worker"),
    ]
    assert released == [("spawn-1", "backend"), ("spawn-1", "worker")]


def test_terminate_spawn_scopes_skips_released_and_session_owned_scopes(
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
        lambda root, spawn_id, scope_id: scope_id == "released",
    )
    # PROC-007: should_skip_cleanup now validates the process is alive.
    # Fake pid=202 as alive with a matching birth time so the scope is preserved.
    monkeypatch.setattr(
        process_cleanup.psutil,
        "Process",
        lambda pid: type("LiveProc", (), {"create_time": lambda self: 10.0})(),
    )

    def _unexpected(**kwargs):
        raise AssertionError("terminate_tree_sync should not be called for skipped scopes")

    monkeypatch.setattr(process_cleanup, "terminate_tree_sync", _unexpected)

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
    monkeypatch.setattr(
        process_cleanup,
        "terminate_tree_sync",
        lambda **kwargs: legacy_result,
    )

    results = terminate_spawn_scopes(tmp_path, _record(worker_pid=321), reason="reaper")

    assert results == [legacy_result]


def test_terminate_spawn_scopes_logs_proc_011_fields_for_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    scope = _scope("backend", pid=101)
    logged: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        process_cleanup,
        "read_scopes_from_disk",
        lambda root, spawn_id: [scope],
    )
    monkeypatch.setattr(
        process_cleanup,
        "is_scope_released",
        lambda root, spawn_id, scope_id: False,
    )
    monkeypatch.setattr(
        process_cleanup,
        "mark_scope_released",
        lambda root, spawn_id, scope_id: None,
    )
    monkeypatch.setattr(
        process_cleanup,
        "terminate_tree_sync",
        lambda **kwargs: CleanupResult(
            scope_id="backend",
            root_pid=101,
            descendant_count=3,
            reason="reaper",
            grace_seconds=4.0,
            kill_escalated=True,
            degraded_fallback=False,
            skip_reason=None,
        ),
    )
    monkeypatch.setattr(
        process_cleanup.logger,
        "info",
        lambda event, **kwargs: logged.append((event, kwargs)),
    )

    terminate_spawn_scopes(tmp_path, _record(), reason="reaper", grace_seconds=4.0)

    assert logged[-1] == (
        "Terminated process scope.",
        {
            "spawn_id": "spawn-1",
            "scope_id": "backend",
            "root_pid": 101,
            "descendant_count": 3,
            "reason": "reaper",
            "grace_seconds": 4.0,
            "kill_escalated": True,
            "degraded_fallback": False,
            "skip_reason": None,
        },
    )


def test_terminate_spawn_scopes_logs_proc_011_fields_for_skip(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from meridian.lib.core import process_cleanup

    scope = _scope(
        "backend",
        owner_policy="session_owned",
        owner_id="session-9",
        pid=202,
    )
    logged: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        process_cleanup,
        "read_scopes_from_disk",
        lambda root, spawn_id: [scope],
    )
    monkeypatch.setattr(
        process_cleanup,
        "is_scope_released",
        lambda root, spawn_id, scope_id: False,
    )
    monkeypatch.setattr(
        process_cleanup.psutil,
        "Process",
        lambda pid: type("LiveProc", (), {"create_time": lambda self: 10.0})(),
    )
    monkeypatch.setattr(
        process_cleanup.logger,
        "info",
        lambda event, **kwargs: logged.append((event, kwargs)),
    )

    terminate_spawn_scopes(tmp_path, _record(), reason="reaper", grace_seconds=4.0)

    assert logged[-1] == (
        "Skipped process scope cleanup — session lease active.",
        {
            "spawn_id": "spawn-1",
            "scope_id": "backend",
            "root_pid": 202,
            "descendant_count": None,
            "reason": "reaper",
            "grace_seconds": 0.0,
            "kill_escalated": False,
            "degraded_fallback": False,
            "skip_reason": "active_session_lease",
        },
    )


# ---------------------------------------------------------------------------
# PROC-007: should_skip_cleanup — session_owned process-alive validation
# ---------------------------------------------------------------------------


def test_should_skip_cleanup_session_owned_process_dead_returns_false(
    monkeypatch,
) -> None:
    """PROC-007: session_owned scope whose root process is dead must NOT be
    skipped — the scope must be reclaimed despite the session_owned policy.
    """
    from meridian.lib.core import process_cleanup

    scope = _scope("backend", owner_policy="session_owned", owner_id="session-abc", pid=12345)
    record = _record()

    def _raise_no_such_process(pid: int) -> None:
        raise _psutil.NoSuchProcess(pid=pid)

    monkeypatch.setattr(process_cleanup.psutil, "Process", _raise_no_such_process)

    result = should_skip_cleanup(scope, record)

    assert result is False


def test_should_skip_cleanup_session_owned_pid_reused_returns_false(
    monkeypatch,
) -> None:
    """PROC-007 + PROC-006: If the root PID was recycled (birth time mismatch),
    must NOT skip — the original process is gone.
    """
    from meridian.lib.core import process_cleanup

    scope = _scope(
        "backend",
        owner_policy="session_owned",
        owner_id="session-abc",
        pid=12345,
        root_created_at_epoch=1_000_000.0,
    )
    record = _record()

    alive_proc = MagicMock()
    alive_proc.create_time.return_value = 9_999_999.0  # far from expected 1_000_000.0

    monkeypatch.setattr(process_cleanup.psutil, "Process", lambda pid: alive_proc)

    result = should_skip_cleanup(scope, record)

    assert result is False


def test_should_skip_cleanup_session_owned_alive_returns_true(
    monkeypatch,
) -> None:
    """PROC-007: session_owned scope with alive + verified root process must be
    preserved (return True).
    """
    from meridian.lib.core import process_cleanup

    created = 1_000_000.0
    scope = _scope(
        "backend",
        owner_policy="session_owned",
        owner_id="session-abc",
        pid=12345,
        root_created_at_epoch=created,
    )
    record = _record()

    alive_proc = MagicMock()
    alive_proc.create_time.return_value = created + 0.1  # within 1.0 s tolerance

    monkeypatch.setattr(process_cleanup.psutil, "Process", lambda pid: alive_proc)

    result = should_skip_cleanup(scope, record)

    assert result is True


def test_should_skip_cleanup_spawn_owned_always_returns_false() -> None:
    """spawn_owned scopes must never be skipped — no process check needed."""
    scope = _scope("worker", owner_policy="spawn_owned", owner_id="spawn-1", pid=55555)
    record = _record()

    # should_skip_cleanup must short-circuit before calling psutil.
    # No monkeypatch of psutil — if psutil.Process is called, it would look up
    # a real pid (likely non-existent) and raise NoSuchProcess; that would also
    # return False, but we verify correctness via the policy short-circuit.
    result = should_skip_cleanup(scope, record)

    assert result is False
