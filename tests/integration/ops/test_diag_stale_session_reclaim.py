"""Doctor/background stale-session repair reclaims managed-primary scopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from meridian.lib.core import process_cleanup
from meridian.lib.core.types import SpawnId
from meridian.lib.ops import diag
from meridian.lib.platform.process_scope.base import (
    PROCESS_BIRTH_UNKNOWN_EPOCH,
    CleanupResult,
    ProcessScopeSnapshot,
)
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.process_scope_projection import record_scope

if TYPE_CHECKING:
    import pytest


class _FakeProcess:
    def __init__(self, created_at: float) -> None:
        self._created_at = created_at

    def create_time(self) -> float:
        return self._created_at


def _write_primary_session_with_lease(
    runtime_root: Path,
    *,
    chat_id: str,
    owner_pid: int,
    generation: str = "gen-1",
) -> None:
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / "sessions.jsonl").write_text(
        json.dumps(
            {
                "v": 1,
                "event": "start",
                "chat_id": chat_id,
                "kind": "primary",
                "harness": "opencode",
                "harness_session_id": "thread-1",
                "model": "gpt-5.4",
                "session_instance_id": generation,
                "started_at": "2026-03-01T00:00:00Z",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    lock_path = runtime_root / "sessions" / f"{chat_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    (runtime_root / "sessions" / f"{chat_id}.lease.json").write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "owner_pid": owner_pid,
                "owner_created_at_epoch": float(owner_pid),
                "session_instance_id": generation,
            }
        ),
        encoding="utf-8",
    )


def _create_primary_spawn_with_scope(
    runtime_root: Path,
    *,
    spawn_id: str,
    chat_id: str,
    pid: int,
    created_at: float,
) -> ProcessScopeSnapshot:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id=chat_id,
        model="gpt-5.4",
        agent="tester",
        harness="opencode",
        kind="primary",
        prompt="hello",
        worker_pid=222,
        runner_pid=111,
        started_at="2026-03-01T00:00:00Z",
        status="running",
    )
    scope = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="session_owned",
        owner_id="thread-1",
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=pid,
        root_created_at_epoch=created_at,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )
    record_scope(runtime_root, SpawnId(spawn_id), scope)
    return scope


def _cleanup_result(
    scope: ProcessScopeSnapshot,
    reason: str,
    skip_reason: str | None,
) -> CleanupResult:
    return CleanupResult(
        scope_id=scope.scope_id,
        root_pid=scope.root_pid,
        descendant_count=None if skip_reason else 0,
        reason=reason,
        grace_seconds=5.0,
        kill_escalated=False,
        degraded_fallback=False,
        skip_reason=skip_reason,
    )


def test_stale_session_repair_reclaims_matching_session_owned_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    _write_primary_session_with_lease(runtime_root, chat_id="chat-1", owner_pid=9001)
    scope = _create_primary_spawn_with_scope(
        runtime_root,
        spawn_id="spawn-1",
        chat_id="chat-1",
        pid=7001,
        created_at=123.0,
    )
    monkeypatch.setattr(
        session_store,
        "is_process_alive_with_birth",
        lambda _pid, _birth: False,
    )
    monkeypatch.setattr(process_cleanup.psutil, "Process", lambda _pid: _FakeProcess(123.0))
    killed: list[int] = []

    def _terminate_scope_sync(
        candidate: ProcessScopeSnapshot,
        *,
        grace_seconds: float,
        reason: str,
    ) -> CleanupResult:
        assert grace_seconds == 5.0
        killed.append(candidate.root_pid)
        return _cleanup_result(candidate, reason, None)

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        _terminate_scope_sync,
    )

    cleaned = diag._repair_stale_session_locks(tmp_path, runtime_root=runtime_root)

    assert cleaned == 1
    assert killed == [scope.root_pid]


def test_stale_session_repair_pid_reuse_result_does_not_kill_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    _write_primary_session_with_lease(runtime_root, chat_id="chat-2", owner_pid=9002)
    _create_primary_spawn_with_scope(
        runtime_root,
        spawn_id="spawn-2",
        chat_id="chat-2",
        pid=7002,
        created_at=123.0,
    )
    monkeypatch.setattr(
        session_store,
        "is_process_alive_with_birth",
        lambda _pid, _birth: False,
    )
    monkeypatch.setattr(process_cleanup.psutil, "Process", lambda _pid: _FakeProcess(999.0))
    killed: list[int] = []

    def _terminate_scope_sync(
        _candidate: ProcessScopeSnapshot,
        *,
        _grace_seconds: float,
        _reason: str,
    ) -> CleanupResult:
        raise AssertionError("PID-reused scope must not reach the kill primitive")

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        _terminate_scope_sync,
    )

    cleaned = diag._repair_stale_session_locks(tmp_path, runtime_root=runtime_root)

    assert cleaned == 1
    assert killed == []


def test_stale_session_repair_unknown_birth_does_not_kill_live_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    _write_primary_session_with_lease(runtime_root, chat_id="chat-unknown", owner_pid=9004)
    _create_primary_spawn_with_scope(
        runtime_root,
        spawn_id="spawn-unknown",
        chat_id="chat-unknown",
        pid=7004,
        created_at=PROCESS_BIRTH_UNKNOWN_EPOCH,
    )
    monkeypatch.setattr(
        session_store,
        "is_process_alive_with_birth",
        lambda _pid, _birth: False,
    )
    monkeypatch.setattr(process_cleanup.psutil, "Process", lambda _pid: _FakeProcess(123.0))

    def _terminate_scope_sync(
        _candidate: ProcessScopeSnapshot,
        *,
        grace_seconds: float,
        reason: str,
    ) -> CleanupResult:
        _ = (grace_seconds, reason)
        raise AssertionError("unknown-birth crash repair must not reach the kill primitive")

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        _terminate_scope_sync,
    )

    cleaned = diag._repair_stale_session_locks(tmp_path, runtime_root=runtime_root)

    assert cleaned == 1


def test_live_session_repair_does_not_reclaim_session_owned_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    _write_primary_session_with_lease(runtime_root, chat_id="chat-3", owner_pid=9003)
    _create_primary_spawn_with_scope(
        runtime_root,
        spawn_id="spawn-3",
        chat_id="chat-3",
        pid=7003,
        created_at=123.0,
    )
    monkeypatch.setattr(
        session_store,
        "is_process_alive_with_birth",
        lambda pid, birth: pid == 9003 and birth == 9003.0,
    )
    monkeypatch.setattr(
        process_cleanup.psutil,
        "Process",
        lambda _pid: (_ for _ in ()).throw(psutil.NoSuchProcess(_pid)),
    )

    def _unexpected_terminate_scope_sync(*_args: object, **_kwargs: object) -> CleanupResult:
        raise AssertionError("live session scopes must not be reclaimed")

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        _unexpected_terminate_scope_sync,
    )

    cleaned = diag._repair_stale_session_locks(tmp_path, runtime_root=runtime_root)

    assert cleaned == 0
