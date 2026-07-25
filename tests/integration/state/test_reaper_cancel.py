# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false
# qa-validated: test-suite-redesign

"""spawn_cancel_sync flows for managed-primary and orphan-primary spawns.

Covers: cancel after passive reconcile, unreadable-metadata worker-pid
fallback, terminal-non-orphan no-op, launcher-signal ordering, queued
timeout convergence.  Reconciliation logic lives in
test_reaper_reconciliation.py; managed-primary detection in
test_reaper_managed_primary.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, cast

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import SpawnId
from meridian.lib.ops.spawn.models import SpawnCancelInput
from meridian.lib.state import spawn_store, spawn_tree
from tests.integration.state.conftest import (
    _OLD_STARTED_AT,
    _create_spawn,
    _get_spawn,
    _reconcile,
    _write_corrupt_primary_meta,
    _write_primary_meta,
    fake_managed_primary_birth_liveness,
    fake_reaper_liveness,
    recording_managed_primary_terminations,
    recording_scope_cleanup,
)


def _patch_spawn_cancel_runtime_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_root: Path,
    spawn_id: str,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.resolve_runtime_root_and_config",
        lambda _project_root: (runtime_root, object()),
    )
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.resolve_runtime_root",
        lambda _project_root: runtime_root,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.resolve_spawn_reference",
        lambda _project_root, _spawn_id: spawn_id,
    )


def _start_descendant(
    runtime_root: Path,
    spawn_id: str,
    *,
    parent_id: str | None = None,
    launch_mode: str = "foreground",
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id=spawn_id,
        parent_id=parent_id,
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt=f"prompt {spawn_id}",
        status="running",
        launch_mode=launch_mode,
    )


def _spawn_service(runtime_root: Path) -> SpawnApplicationService:
    return SpawnApplicationService(
        runtime_root,
        SpawnLifecycleService(runtime_root),
    )


def _write_session_lease(runtime_root: Path, *, chat_id: str = "c1", owner_pid: int = 8101) -> None:
    sessions_dir = runtime_root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{chat_id}.lease.json").write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "owner_pid": owner_pid,
                "session_instance_id": "session-instance-1",
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_cancel_descendants_rescans_for_newly_appearing_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / ".meridian"
    _start_descendant(runtime_root, "p1")
    _start_descendant(runtime_root, "p2", parent_id="p1")
    service = _spawn_service(runtime_root)
    original_active_descendants = spawn_tree.active_descendants
    scans = 0

    def active_descendants(runtime_root_arg: Path, root_id: SpawnId | str):
        nonlocal scans
        scans += 1
        if scans == 2:
            _start_descendant(runtime_root, "p3", parent_id="p2")
        return original_active_descendants(runtime_root_arg, root_id)

    monkeypatch.setattr(spawn_tree, "active_descendants", active_descendants)

    reaped_ids = await service.cancel_descendants(SpawnId("p1"))

    child = spawn_store.get_spawn(runtime_root, "p2")
    grandchild = spawn_store.get_spawn(runtime_root, "p3")
    assert child is not None
    assert child.status == "cancelled"
    assert grandchild is not None
    assert grandchild.status == "cancelled"
    assert reaped_ids == {"p2", "p3"}


@pytest.mark.asyncio
async def test_cancel_descendants_reports_only_proven_terminal_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / ".meridian"
    _start_descendant(runtime_root, "p1")
    _start_descendant(runtime_root, "p2", parent_id="p1", launch_mode="app")
    _start_descendant(runtime_root, "p3", parent_id="p1")

    class NonterminatingManager:
        async def stop_spawn(self, *_args: object, **_kwargs: object) -> None:
            return None

    service = SpawnApplicationService(
        runtime_root,
        SpawnLifecycleService(runtime_root),
        spawn_manager=cast("Any", NonterminatingManager()),
    )

    async def never_terminal(*_args: object, **_kwargs: object) -> Any:
        return None

    monkeypatch.setattr(service, "_wait_for_terminal", never_terminal)

    reaped_ids = await service.cancel_descendants(SpawnId("p1"))

    nonterminal = spawn_store.get_spawn(runtime_root, "p2")
    terminal = spawn_store.get_spawn(runtime_root, "p3")
    assert nonterminal is not None
    assert nonterminal.status == "running"
    assert nonterminal.cancel_intent is not None
    assert terminal is not None
    assert terminal.status == "cancelled"
    assert reaped_ids == {"p3"}


def test_cancel_orphan_primary_after_passive_reconcile_still_terminates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7301,
        backend_pid=7302,
        tui_pid=7303,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    fake_reaper_liveness(monkeypatch, set())
    fake_managed_primary_birth_liveness(monkeypatch, {7302, 7303})
    terminated_pids = recording_managed_primary_terminations(monkeypatch)

    reconciled = _reconcile(tmp_path, runtime_root, _get_spawn(runtime_root, spawn_id))

    assert reconciled.status == "failed"
    assert reconciled.terminal is not None
    assert reconciled.terminal.exit_code == 1
    assert reconciled.terminal is not None
    assert reconciled.terminal.error == "orphan_primary"
    assert terminated_pids == [7302, 7303]

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "failed"
    assert output.exit_code == 1
    assert terminated_pids == [7302, 7303, 7302, 7303]
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.terminal is not None
    assert latest.terminal.error == "orphan_primary"


def test_cancel_orphan_primary_candidate_with_unreadable_metadata_uses_worker_pid_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_pid = 9351
    worker_pid = 9352
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        harness="codex",
        runner_pid=runner_pid,
        worker_pid=worker_pid,
        started_at=_OLD_STARTED_AT,
    )
    _write_corrupt_primary_meta(runtime_root, spawn_id)

    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    fake_reaper_liveness(monkeypatch, {worker_pid})
    passive_terminated_pids = recording_scope_cleanup(
        monkeypatch,
        "meridian.lib.core.process_cleanup.terminate_tree_sync",
    )

    reconciled = _reconcile(tmp_path, runtime_root, _get_spawn(runtime_root, spawn_id))

    assert reconciled.status == "failed"
    assert reconciled.terminal is not None
    assert reconciled.terminal.exit_code == 1
    assert reconciled.terminal is not None
    assert reconciled.terminal.error == "orphan_primary"
    assert passive_terminated_pids == [worker_pid]

    explicit_terminated_pids = recording_scope_cleanup(
        monkeypatch,
        "meridian.lib.core.process_cleanup.terminate_tree_sync",
    )

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "failed"
    assert output.exit_code == 1
    assert explicit_terminated_pids == [worker_pid]
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.terminal is not None
    assert latest.terminal.error == "orphan_primary"


def test_spawn_cancel_managed_primary_signals_launcher_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7001,
        backend_pid=7002,
        tui_pid=7003,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive",
        lambda pid, created_after_epoch=None: pid == 7001,
    )
    fake_managed_primary_birth_liveness(monkeypatch, {7001, 7002, 7003})
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_GRACE_SECS", 0.01)
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_FALLBACK_WAIT_SECS", 0.01)
    terminated_pids = recording_managed_primary_terminations(monkeypatch)

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "finalizing"
    assert output.exit_code == 130
    assert terminated_pids[0] == 7001
    assert set(terminated_pids[1:]) == {7002, 7003}
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "finalizing"
    assert latest.cancel_intent is not None
    assert latest.cancel_intent.exit_code == 130
    assert latest.cancel_intent.error == "cancelled"


def test_spawn_cancel_refuses_live_managed_primary_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        started_at=_OLD_STARTED_AT,
    )
    _write_session_lease(runtime_root)
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.state.session_store.is_process_alive",
        lambda pid: pid == 8101,
    )

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "failed"
    assert output.exit_code == 1
    assert output.error is not None
    assert "live managed primary" in output.error
    assert "agent: tester" in output.error
    assert "chat: c1" in output.error
    assert "uptime:" in output.error
    assert f"meridian spawn cancel {spawn_id} --force" in output.error
    assert _get_spawn(runtime_root, spawn_id).cancel_intent is None


@pytest.mark.parametrize("lease_content", [None, "{corrupt"], ids=["missing", "corrupt"])
def test_spawn_cancel_refuses_process_verified_live_primary_without_readable_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lease_content: str | None,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        started_at=_OLD_STARTED_AT,
    )
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=8201,
        launcher_birth_epoch=8201.0,
        backend_pid=None,
        tui_pid=None,
        activity="idle",
    )
    if lease_content is not None:
        lease_path = runtime_root / "sessions" / "c1.lease.json"
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(lease_content, encoding="utf-8")
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive_with_birth",
        lambda pid, birth: pid == 8201 and birth == 8201.0,
    )

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "failed"
    assert output.exit_code == 1
    assert output.error is not None
    assert "live managed primary" in output.error
    assert _get_spawn(runtime_root, spawn_id).cancel_intent is None


def test_spawn_cancel_allows_dead_managed_primary_without_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        status="queued",
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=8202,
        backend_pid=None,
        tui_pid=None,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive_with_birth",
        lambda _pid, _birth: False,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "cancelled"
    assert output.exit_code == 130
    assert _get_spawn(runtime_root, spawn_id).status == "cancelled"


@pytest.mark.parametrize(
    ("force", "lease_owner_alive"),
    [(True, True), (False, False)],
    ids=["force-live-primary", "dead-primary-without-force"],
)
def test_spawn_cancel_allows_forced_or_dead_managed_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    force: bool,
    lease_owner_alive: bool,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        status="queued",
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    _write_session_lease(runtime_root)
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=None,
        backend_pid=None,
        tui_pid=None,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.state.session_store.is_process_alive",
        lambda pid: pid == 8101 and lease_owner_alive,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_GRACE_SECS", 0.01)
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_FALLBACK_WAIT_SECS", 0.01)

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
            force=force,
        )
    )

    assert output.status == "cancelled"
    assert output.exit_code == 130
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "cancelled"
    assert latest.cancel_intent is not None


def test_spawn_cancel_returns_promptly_when_managed_primary_is_already_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    _write_session_lease(runtime_root)
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=None,
        backend_pid=None,
        tui_pid=None,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.state.session_store.is_process_alive",
        lambda _pid: False,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    started = time.monotonic()
    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )
    elapsed = time.monotonic() - started

    assert output.status == "cancelled"
    assert elapsed < 0.5


def test_spawn_cancel_managed_primary_queued_converges_to_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        status="queued",
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7201,
        backend_pid=7202,
        tui_pid=7203,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    fake_managed_primary_birth_liveness(monkeypatch, set())
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_GRACE_SECS", 0.01)
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_FALLBACK_WAIT_SECS", 0.01)
    terminated_pids = recording_managed_primary_terminations(monkeypatch)

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "cancelled"
    assert output.exit_code == 130
    assert terminated_pids == []
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "cancelled"
    assert latest.terminal is not None
    assert latest.terminal.error == "cancelled"
    assert latest.terminal.origin == "cancel"
