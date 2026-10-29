"""Orphan-run repair captures the native transcript of a reconciled primary."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.ops import diag
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import LAUNCH_FILE_NAMES, resolve_project_runtime_root_for_write
from meridian.lib.state.process_scope_projection import record_scope
from meridian.lib.state.reaper_cleanup_claim import claim_active_spawn_scopes
from meridian.lib.state.spawn.model import SpawnKind, SpawnRecord
from tests.support.launch_paths import launch_paths

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


def test_repair_reconciles_active_orphan_and_terminal_cleanup_claim(
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
        (runtime_root / "spawns" / spawn_id / "launch.lock").touch()

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
    record_scope(launch_paths(runtime_root, SpawnId("p2")), scope)
    claim_active_spawn_scopes(runtime_root, SpawnId("p2"))
    spawn_store.finalize_spawn(
        runtime_root,
        "p2",
        "failed",
        1,
        origin="reconciler",
        error="orphan_run",
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

    def _capture_then_fail(_project: Path, _runtime: Path, spawn_id: str) -> None:
        # A failed capture must not undo the repair that already published.
        captured.append(spawn_id)
        raise ValueError("incomplete native tail")

    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.materialize_native_history",
        _capture_then_fail,
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


def test_repair_captures_every_orphan_primary_of_one_native_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crashed ``--continue`` generations share a native id; one pass captures all.

    The real capture guard defers while any row bound to the native session is
    still active, so every orphan must be reconciled before any capture runs.
    """
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project)
    native_root = resolve_pi_spawn_session_root()
    native_root.mkdir(parents=True)
    (native_root / "timestamp_resumed-native.jsonl").write_text(
        json.dumps({"type": "session", "version": 3, "id": "resumed-native"}) + "\n"
    )
    spawn_ids = ("p1", "p2", "p3")
    for spawn_id in spawn_ids:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id=spawn_id,
            chat_id="c1",
            harness="pi",
            harness_session_id="resumed-native",
            kind="primary",
            prompt="continued",
            model="test",
            agent="coder",
            worker_pid=222,
            runner_pid=111,
            started_at="2000-01-01T00:00:00Z",
        )
        (runtime_root / "spawns" / spawn_id / "launch.lock").touch()
        session_store.start_session(
            runtime_root,
            "pi",
            "resumed-native",
            "test",
            chat_id="c1",
            kind="primary",
            spawn_id=spawn_id,
            native_store=str(native_root),
        )
        session_store.stop_session(runtime_root, "c1")
    count, ids = diag._repair_orphan_runs(project, runtime_root=runtime_root)

    assert (count, ids) == (3, spawn_ids)
    for spawn_id in spawn_ids:
        record = spawn_store.get_spawn(runtime_root, spawn_id)
        assert record is not None and record.status == "failed"
        assert (runtime_root / "spawns" / spawn_id / LAUNCH_FILE_NAMES.native_snapshot).is_file()
