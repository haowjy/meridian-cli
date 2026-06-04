# qa-validated: reaper-escape-fix-test-cleanup
from __future__ import annotations

from pathlib import Path

import pytest  # noqa: TC002

from meridian.lib.core.process_cleanup import reclaim_session_owned_scopes_for_chat
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.process_scope_projection import (
    is_scope_released,
    mark_scope_released,
    record_scope,
)
from meridian.lib.state.reaper import reconcile_active_spawn
from meridian.lib.state.spawn.model import SpawnRecord

_OLD_STARTED_AT = "2000-01-01T00:00:00Z"


def _runtime_root(tmp_path: Path) -> Path:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _create_primary_spawn(tmp_path: Path, spawn_id: str = "p1") -> tuple[Path, str]:
    runtime_root = _runtime_root(tmp_path)
    created = spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.4",
        agent="tester",
        harness="codex",
        kind="primary",
        prompt="hello",
        worker_pid=222,
        runner_pid=111,
        started_at=_OLD_STARTED_AT,
        status="running",
    )
    return runtime_root, str(created)


def _get_spawn(runtime_root: Path, spawn_id: str) -> SpawnRecord:
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert record is not None
    return record


def _scope(
    spawn_id: str,
    *,
    scope_id: str,
    owner_policy: str,
    owner_id: str,
    root_pid: int,
) -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy=owner_policy,
        owner_id=owner_id,
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=root_pid,
        root_created_at_epoch=100.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )


def test_reclaim_session_owned_scopes_for_chat_reclaims_all_matching_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session-exit reclaim should fan out across every spawn in the chat."""
    runtime_root, spawn_one = _create_primary_spawn(tmp_path, spawn_id="p1")
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p2",
        chat_id="c1",
        model="gpt-5.4",
        agent="tester",
        harness="codex",
        kind="primary",
        prompt="hello again",
        worker_pid=333,
        runner_pid=222,
        started_at=_OLD_STARTED_AT,
        status="running",
    )

    scope_records = (
        (
            spawn_one,
            _scope(
                spawn_one,
                scope_id="backend",
                owner_policy="session_owned",
                owner_id="session-a",
                root_pid=301,
            ),
        ),
        (
            spawn_one,
            _scope(
                spawn_one,
                scope_id="spawn-owned",
                owner_policy="spawn_owned",
                owner_id=spawn_one,
                root_pid=302,
            ),
        ),
        (
            "p2",
            _scope(
                "p2",
                scope_id="tui",
                owner_policy="session_owned",
                owner_id="session-b",
                root_pid=401,
            ),
        ),
        (
            "p2",
            _scope(
                "p2",
                scope_id="already-released",
                owner_policy="session_owned",
                owner_id="session-c",
                root_pid=402,
            ),
        ),
    )
    for spawn_id, scope in scope_records:
        record_scope(runtime_root, SpawnId(spawn_id), scope)

    already_released_scope = scope_records[3][1]
    mark_scope_released(runtime_root, SpawnId("p2"), already_released_scope.release_id)

    terminated_scope_ids: list[str] = []

    def _fake_terminate_scope_sync(
        scope: ProcessScopeSnapshot,
        *,
        grace_seconds: float,
        reason: str,
    ) -> CleanupResult:
        terminated_scope_ids.append(scope.scope_id)
        return CleanupResult(
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=0,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason=None,
        )

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        _fake_terminate_scope_sync,
    )

    results = reclaim_session_owned_scopes_for_chat(runtime_root, "c1", grace_seconds=2.0)

    assert terminated_scope_ids == ["backend", "tui"]
    assert [result.scope_id for result in results] == ["backend", "tui"]
    assert (
        is_scope_released(runtime_root, SpawnId(spawn_one), scope_records[0][1].release_id)
        is True
    )
    assert is_scope_released(runtime_root, SpawnId("p2"), scope_records[2][1].release_id) is True
    assert is_scope_released(runtime_root, SpawnId("p2"), already_released_scope.release_id) is True


def test_duplicate_scope_labels_are_released_by_concrete_identity(tmp_path: Path) -> None:
    runtime_root, spawn_id = _create_primary_spawn(tmp_path)
    first = _scope(
        spawn_id,
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id=spawn_id,
        root_pid=501,
    )
    second = _scope(
        spawn_id,
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id=spawn_id,
        root_pid=502,
    )

    record_scope(runtime_root, SpawnId(spawn_id), first)
    record_scope(runtime_root, SpawnId(spawn_id), second)
    mark_scope_released(runtime_root, SpawnId(spawn_id), first.release_id)

    assert first.scope_id == second.scope_id
    assert first.release_id != second.release_id
    assert is_scope_released(runtime_root, SpawnId(spawn_id), first.release_id) is True
    assert is_scope_released(runtime_root, SpawnId(spawn_id), second.release_id) is False


def test_reaper_preserve_then_reclaim_reuses_same_scope_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_primary_spawn(tmp_path)
    session_id = "session-1"
    scope_records = (
        _scope(
            spawn_id,
            scope_id="launcher",
            owner_policy="spawn_owned",
            owner_id=spawn_id,
            root_pid=300,
        ),
        _scope(
            spawn_id,
            scope_id="backend",
            owner_policy="session_owned",
            owner_id=session_id,
            root_pid=301,
        ),
        _scope(
            spawn_id,
            scope_id="tui",
            owner_policy="session_owned",
            owner_id=session_id,
            root_pid=302,
        ),
    )
    for scope in scope_records:
        record_scope(runtime_root, SpawnId(spawn_id), scope)

    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    alive_proc = type("AliveProc", (), {"create_time": lambda self: 100.0})()
    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.psutil.Process",
        lambda _pid: alive_proc,
    )

    terminated_scope_ids: list[str] = []

    def _fake_terminate_scope_sync(
        scope: ProcessScopeSnapshot,
        *,
        grace_seconds: float,
        reason: str,
    ) -> CleanupResult:
        terminated_scope_ids.append(scope.scope_id)
        return CleanupResult(
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=0,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason=None,
        )

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        _fake_terminate_scope_sync,
    )

    reconciled = reconcile_active_spawn(tmp_path, runtime_root, _get_spawn(runtime_root, spawn_id))

    assert terminated_scope_ids == ["launcher"]
    assert reconciled.status == "failed"
    assert reconciled.error == "orphan_primary"
    assert is_scope_released(runtime_root, SpawnId(spawn_id), scope_records[0].release_id) is True
    assert is_scope_released(runtime_root, SpawnId(spawn_id), scope_records[1].release_id) is False
    assert is_scope_released(runtime_root, SpawnId(spawn_id), scope_records[2].release_id) is False

    terminated_scope_ids.clear()
    results = reclaim_session_owned_scopes_for_chat(runtime_root, "c1", grace_seconds=2.0)

    assert terminated_scope_ids == ["backend", "tui"]
    assert [result.scope_id for result in results] == ["backend", "tui"]
    assert is_scope_released(runtime_root, SpawnId(spawn_id), scope_records[1].release_id) is True
    assert is_scope_released(runtime_root, SpawnId(spawn_id), scope_records[2].release_id) is True
