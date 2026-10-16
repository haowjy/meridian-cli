"""Orphan-run repair captures the native transcript of a reconciled primary."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog.testing

from meridian.lib.core.types import SpawnId
from meridian.lib.ops import diag
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state import spawn_store
from meridian.lib.state.process_scope_projection import record_scope
from meridian.lib.state.reaper_cleanup_claim import claim_active_spawn_scopes
from meridian.lib.state.spawn.model import SpawnKind, SpawnRecord

if TYPE_CHECKING:
    from pathlib import Path


def _install_reconciled_orphan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: SpawnKind,
) -> None:
    running = SpawnRecord(id="p1", kind=kind, status="running")
    terminal = SpawnRecord(id="p1", kind=kind, status="cancelled")
    monkeypatch.setattr(
        "meridian.lib.state.reaper.iter_repair_candidates",
        lambda _root: iter((running,)),
    )
    monkeypatch.setattr(
        "meridian.lib.state.reaper.reconcile_active_spawn",
        lambda *_args, **_kwargs: terminal,
    )


def test_reconciled_primary_orphan_is_captured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reconciled_orphan(monkeypatch, kind="primary")
    captured: list[tuple[Path, Path, str]] = []
    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.materialize_native_history",
        lambda *args: captured.append(args),
    )

    runtime_root = tmp_path / "runtime"
    count, ids = diag._repair_orphan_runs(tmp_path, runtime_root=runtime_root)

    assert count == 1
    assert ids == ("p1",)
    assert captured == [(tmp_path, runtime_root, "p1")]


def test_reconciled_child_orphan_is_not_captured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reconciled_orphan(monkeypatch, kind="child")
    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.materialize_native_history",
        lambda *_args: pytest.fail("only primaries capture native history"),
    )

    count, ids = diag._repair_orphan_runs(tmp_path, runtime_root=tmp_path / "runtime")

    assert count == 1
    assert ids == ("p1",)


def test_orphan_capture_failure_does_not_break_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reconciled_orphan(monkeypatch, kind="primary")

    def _raising(*_args: object) -> None:
        raise ValueError("incomplete native tail")

    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.materialize_native_history",
        _raising,
    )

    count, ids = diag._repair_orphan_runs(tmp_path, runtime_root=tmp_path / "runtime")

    assert count == 1
    assert ids == ("p1",)


def test_orphan_capture_deferral_is_debug_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.ops.session_archive import NativeCaptureDeferred

    _install_reconciled_orphan(monkeypatch, kind="primary")

    def _deferred(*_args: object) -> None:
        raise NativeCaptureDeferred("spawn p2")

    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.materialize_native_history",
        _deferred,
    )
    with structlog.testing.capture_logs() as logs:
        count, ids = diag._repair_orphan_runs(tmp_path, runtime_root=tmp_path / "runtime")

    assert count == 1
    assert ids == ("p1",)
    assert logs == [
        {
            "event": "orphan_primary_capture_deferred",
            "log_level": "debug",
            "owner": "spawn p2",
            "spawn_id": "p1",
        }
    ]


def test_repair_stream_reconciles_active_orphan_and_terminal_cleanup_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    for spawn_id, kind in (("p1", "primary"), ("p2", "child")):
        spawn_store.start_spawn(
            runtime_root,
            spawn_id=spawn_id,
            chat_id=f"c{spawn_id[1:]}",
            model="test",
            agent="tester",
            harness="codex",
            kind=kind,
            prompt="hello",
            worker_pid=222,
            runner_pid=111,
            started_at="2000-01-01T00:00:00Z",
            status="running",
        )

    scope = ProcessScopeSnapshot(
        scope_id="worker",
        owner_policy="spawn_owned",
        owner_id="p2",
        role="tool_worker",
        containment="pid_tree_fallback",
        root_pid=222,
        root_created_at_epoch=100.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )
    record_scope(runtime_root, SpawnId("p2"), scope)
    claim_active_spawn_scopes(runtime_root, SpawnId("p2"))
    spawn_store.finalize_spawn(
        runtime_root,
        "p2",
        "failed",
        1,
        origin="reconciler",
        error="orphan_run",
    )

    monkeypatch.setattr(
        spawn_store,
        "list_spawns",
        lambda *_args, **_kwargs: pytest.fail("repair must not materialize all spawns"),
    )
    monkeypatch.setattr("meridian.lib.state.reaper.is_process_alive", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda *_a, **_k: False,
    )
    terminated: list[str] = []

    def terminate(claimed: ProcessScopeSnapshot, **_kwargs: object) -> CleanupResult:
        terminated.append(claimed.release_id)
        return CleanupResult(
            scope_id=claimed.scope_id,
            root_pid=claimed.root_pid,
            descendant_count=0,
            reason="reaper",
            grace_seconds=5.0,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason=None,
        )

    monkeypatch.setattr("meridian.lib.core.process_cleanup.terminate_scope_sync", terminate)
    captured: list[str] = []
    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.materialize_native_history",
        lambda _project, _runtime, spawn_id: captured.append(spawn_id),
    )

    count, ids = diag._repair_orphan_runs(tmp_path, runtime_root=runtime_root)

    repaired = spawn_store.get_spawn(runtime_root, "p1")
    assert repaired is not None
    assert repaired.status == "failed"
    assert count == 1
    assert ids == ("p1",)
    assert captured == ["p1"]
    assert scope.release_id in terminated
    assert not (runtime_root / "spawns" / "p2" / "reaper_cleanup_claim.json").exists()
