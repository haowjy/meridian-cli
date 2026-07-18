from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.process_scope_projection import record_scope
from meridian.lib.state.reaper import reconcile_active_spawn

if TYPE_CHECKING:
    import pytest

_OLD_STARTED_AT = "2000-01-01T00:00:00Z"


def _spawn_with_scope(tmp_path: Path) -> tuple[Path, str, ProcessScopeSnapshot]:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    runtime_root.mkdir(parents=True, exist_ok=True)
    spawn_id = "p1"
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="test",
        agent="tester",
        harness="claude",
        kind="child",
        prompt="hello",
        worker_pid=222,
        runner_pid=111,
        started_at=_OLD_STARTED_AT,
        status="running",
    )
    scope = ProcessScopeSnapshot(
        scope_id="worker",
        owner_policy="spawn_owned",
        owner_id=spawn_id,
        role="tool_worker",
        containment="pid_tree_fallback",
        root_pid=222,
        root_created_at_epoch=100.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )
    record_scope(runtime_root, SpawnId(spawn_id), scope)
    return runtime_root, spawn_id, scope


def _record(runtime_root: Path, spawn_id: str):
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert record is not None
    return record


def _cleanup_result(scope: ProcessScopeSnapshot) -> CleanupResult:
    return CleanupResult(
        scope_id=scope.scope_id,
        root_pid=scope.root_pid,
        descendant_count=0,
        reason="reaper",
        grace_seconds=5.0,
        kill_escalated=False,
        degraded_fallback=False,
        skip_reason=None,
    )


def test_runner_finalize_between_reaper_snapshot_and_cleanup_preserves_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id, _scope = _spawn_with_scope(tmp_path)
    monkeypatch.setattr("meridian.lib.state.reaper.is_process_alive", lambda *_a, **_k: False)
    kill_calls: list[str] = []
    from meridian.lib.state.reaper_cleanup_claim import claim_active_spawn_scopes

    def runner_wins_then_claim(_runtime_root: Path, record, _managed=None, **_kwargs) -> None:
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            "succeeded",
            0,
            origin="runner",
        )
        claim_active_spawn_scopes(_runtime_root, SpawnId(record.id))

    monkeypatch.setattr(
        "meridian.lib.state.reaper._claim_reaper_cleanup",
        runner_wins_then_claim,
    )
    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        lambda *_args, **_kwargs: kill_calls.append("worker"),
    )

    reconciled = reconcile_active_spawn(tmp_path, runtime_root, _record(runtime_root, spawn_id))

    assert reconciled.status == "succeeded"
    assert reconciled.terminal is not None
    assert reconciled.terminal.origin == "runner"
    assert kill_calls == []


def test_reaper_recovers_claim_after_crash_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id, scope = _spawn_with_scope(tmp_path)
    monkeypatch.setattr("meridian.lib.state.reaper.is_process_alive", lambda *_a, **_k: False)
    from meridian.lib.state.reaper import _cleanup_claimed_scopes

    monkeypatch.setattr(
        "meridian.lib.state.reaper._cleanup_claimed_scopes",
        lambda *_args, **_kwargs: None,
    )

    terminal = reconcile_active_spawn(tmp_path, runtime_root, _record(runtime_root, spawn_id))
    claim_path = runtime_root / "spawns" / spawn_id / "reaper_cleanup_claim.json"
    assert terminal.status == "failed"
    assert claim_path.is_file()

    monkeypatch.setattr(
        "meridian.lib.state.reaper._cleanup_claimed_scopes",
        _cleanup_claimed_scopes,
    )
    terminated: list[str] = []

    def terminate(claimed: ProcessScopeSnapshot, **_kwargs) -> CleanupResult:
        terminated.append(claimed.release_id)
        return _cleanup_result(claimed)

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        terminate,
    )
    recovered = reconcile_active_spawn(tmp_path, runtime_root, terminal)

    assert recovered == terminal
    assert terminated == [scope.release_id]
    assert not claim_path.exists()


def test_double_reconcile_does_not_double_kill_claimed_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id, scope = _spawn_with_scope(tmp_path)
    monkeypatch.setattr("meridian.lib.state.reaper.is_process_alive", lambda *_a, **_k: False)
    terminated: list[str] = []

    def terminate(claimed: ProcessScopeSnapshot, **_kwargs) -> CleanupResult:
        terminated.append(claimed.release_id)
        return _cleanup_result(claimed)

    monkeypatch.setattr("meridian.lib.core.process_cleanup.terminate_scope_sync", terminate)
    stale = _record(runtime_root, spawn_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(reconcile_active_spawn, tmp_path, runtime_root, stale)
        second_future = executor.submit(reconcile_active_spawn, tmp_path, runtime_root, stale)
        first = first_future.result()
        second = second_future.result()

    assert first.status == "failed"
    assert second.status == "failed"
    assert terminated == [scope.release_id]
    claim_path = runtime_root / "spawns" / spawn_id / "reaper_cleanup_claim.json"
    assert not claim_path.exists()


def test_cleanup_failure_keeps_only_unresolved_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id, scope = _spawn_with_scope(tmp_path)
    monkeypatch.setattr("meridian.lib.state.reaper.is_process_alive", lambda *_a, **_k: False)

    def fail(claimed: ProcessScopeSnapshot, **_kwargs) -> CleanupResult:
        result = _cleanup_result(claimed)
        return CleanupResult(**{**result.__dict__, "skip_reason": "termination_exception"})

    monkeypatch.setattr("meridian.lib.core.process_cleanup.terminate_scope_sync", fail)
    reconcile_active_spawn(tmp_path, runtime_root, _record(runtime_root, spawn_id))

    claim_path = runtime_root / "spawns" / spawn_id / "reaper_cleanup_claim.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    assert [item["release_id"] for item in payload["scopes"]] == [scope.release_id]
