# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false
# qa-validated: test-suite-redesign

"""Public cancel flows for managed-primary and descendant spawns."""

from __future__ import annotations

import json
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import ExecutionTerminalFacts
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import SpawnId
from meridian.lib.ops.spawn.models import SpawnCancelInput
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from meridian.lib.state import spawn_store, spawn_tree
from meridian.lib.state.process_scope_projection import record_scope
from tests.integration.state.conftest import (
    _OLD_STARTED_AT,
    _create_spawn,
    _get_spawn,
    _write_primary_meta,
    fake_managed_primary_birth_liveness,
    recording_managed_primary_terminations,
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
        "meridian.lib.ops.spawn.api.resolve_spawn_target",
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


def _write_session_lease(
    runtime_root: Path,
    *,
    chat_id: str = "c1",
    owner_pid: int = 8101,
    owner_birth: float = 8101.0,
) -> None:
    sessions_dir = runtime_root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{chat_id}.lease.json").write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "owner_pid": owner_pid,
                "owner_created_at_epoch": owner_birth,
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


@pytest.mark.asyncio
async def test_cancel_descendants_sweeps_dead_child_scope(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".meridian"
    _start_descendant(runtime_root, "p1")
    _start_descendant(runtime_root, "p2", parent_id="p1")
    (runtime_root / "spawns" / "p2" / "launch.lock").touch()
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        record_scope(
            runtime_root,
            SpawnId("p2"),
            ProcessScopeSnapshot(
                scope_id="harness",
                owner_policy="spawn_owned",
                owner_id="p2",
                role="harness_backend",
                containment="pid_tree_fallback",
                root_pid=sleeper.pid,
                root_created_at_epoch=psutil.Process(sleeper.pid).create_time(),
                pgid=None,
                job_name=None,
                degraded_reason=None,
            ),
        )

        reaped_ids = await _spawn_service(runtime_root).cancel_descendants(SpawnId("p1"))

        assert reaped_ids == {"p2"}
        sleeper.wait(timeout=5)
        assert sleeper.poll() is not None
        stored = spawn_store.get_spawn(runtime_root, "p2")
        assert stored is not None
        assert stored.status == "cancelled"
    finally:
        if sleeper.poll() is None:
            with suppress(ProcessLookupError):
                sleeper.kill()
        sleeper.wait(timeout=5)


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
        "meridian.lib.state.session_store.is_process_alive_with_birth",
        lambda pid, birth: pid == 8101 and birth == 8101.0,
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
        "meridian.lib.state.session_store.is_process_alive_with_birth",
        lambda pid, birth: pid == 8101 and birth == 8101.0 and lease_owner_alive,
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
        "meridian.lib.state.session_store.is_process_alive_with_birth",
        lambda _pid, _birth: False,
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


@pytest.mark.asyncio
async def test_complete_execution_cancel_intent_does_not_override_abnormal_exit(
    tmp_path: Path,
) -> None:
    """p6491: a cancel-led permission hang with prose is cancelled, not succeeded."""

    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    service = _spawn_service(runtime_root)

    outcome = await service.complete_execution(
        SpawnId(spawn_id),
        ExecutionTerminalFacts(
            exit_code=143,
            failure_reason="terminated",
            cancellation_observed=True,
            durable_report_completion=True,
        ),
        origin="runner",
    )

    assert (outcome.resolved.status, outcome.resolved.exit_code) == ("cancelled", 143)
    record = _get_spawn(runtime_root, spawn_id)
    assert record.status == "cancelled"
    assert record.terminal is not None
    assert record.terminal.exit_code == 143
