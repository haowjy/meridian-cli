"""Reaper writes are authorized by the runner-lifetime launch lock."""

from __future__ import annotations

import subprocess
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
import pytest

from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import SpawnId
from meridian.lib.ops import diag
from meridian.lib.ops import mars as mars_ops
from meridian.lib.ops.diag import DoctorInput, doctor_sync
from meridian.lib.ops.spawn.api import spawn_show_sync
from meridian.lib.ops.spawn.models import SpawnShowInput
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.primary_meta import PrimaryMetadata, write_primary_metadata
from meridian.lib.state.process_scope_projection import record_scope
from meridian.lib.state.reaper import reconcile_active_spawn
from meridian.lib.state.reaper_cleanup_claim import (
    claim_active_spawn_scopes,
    cleanup_claim_path,
)
from tests.support.launch_paths import launch_paths


def _active_spawn(
    runtime_root: Path,
    *,
    spawn_id: str = "p1",
    kind: str = "child",
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
) -> str:
    created = spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="test",
        agent="tester",
        harness="claude",
        kind=kind,
        prompt="hello",
        runner_pid=runner_pid,
        runner_created_at_epoch=runner_created_at_epoch,
        started_at="2000-01-01T00:00:00Z",
        status="running",
    ).spawn_id
    (runtime_root / "spawns" / str(created) / "launch.lock").touch()
    return str(created)


def _sleep_process() -> tuple[subprocess.Popen[bytes], float]:
    process = subprocess.Popen(["sleep", "30"])
    return process, psutil.Process(process.pid).create_time()


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.kill()
    process.wait(timeout=5)


def test_runner_exit_does_not_turn_a_crashed_launch_into_success(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = _active_spawn(runtime_root)
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status="succeeded",
        exit_code=0,
        exited_at="2000-01-01T00:00:01Z",
    )
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None

    reconciled = reconcile_active_spawn(tmp_path, runtime_root, row)

    assert reconciled.status == "failed"
    assert reconciled.terminal is not None
    assert reconciled.terminal.exit_code == 1
    assert reconciled.terminal.error == "crashed"


def test_dead_runner_with_cancel_intent_is_cancelled(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = _active_spawn(runtime_root)
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled by user",
        requested_at="2000-01-01T00:00:01Z",
    )
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None

    reconciled = reconcile_active_spawn(tmp_path, runtime_root, row)

    assert reconciled.status == "cancelled"
    assert reconciled.terminal is not None
    assert reconciled.terminal.exit_code == 130
    assert reconciled.terminal.error == "cancelled by user"
    assert reconciled.terminal.origin == "reconciler"


@pytest.mark.asyncio
async def test_cancel_dead_runner_with_report_is_cancelled(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = _active_spawn(runtime_root)
    (runtime_root / "spawns" / spawn_id / "report.md").write_text(
        "# Finished\n\nCompleted before the runner crashed.\n",
        encoding="utf-8",
    )
    service = SpawnApplicationService(
        runtime_root,
        SpawnLifecycleService(runtime_root),
    )

    outcome = await service.cancel(SpawnId(spawn_id))

    assert outcome.status == "cancelled"
    persisted = spawn_store.get_spawn(runtime_root, spawn_id)
    assert persisted is not None
    assert persisted.status == "cancelled"
    assert persisted.terminal is not None
    assert persisted.terminal.exit_code == 130
    assert persisted.terminal.error == "cancelled"


def test_reaper_cleans_managed_primary_fallback_processes(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    dead_runner, runner_birth = _sleep_process()
    dead_runner.terminate()
    dead_runner.wait(timeout=5)
    spawn_id = _active_spawn(
        runtime_root,
        kind="primary",
        runner_pid=dead_runner.pid,
        runner_created_at_epoch=runner_birth,
    )
    backend, backend_birth = _sleep_process()
    tui, tui_birth = _sleep_process()
    try:
        write_primary_metadata(
            launch_paths(runtime_root, spawn_id),
            PrimaryMetadata(
                managed_backend=True,
                backend_pid=backend.pid,
                backend_birth_epoch=backend_birth,
                tui_pid=tui.pid,
                tui_birth_epoch=tui_birth,
            ),
        )
        row = spawn_store.get_spawn(runtime_root, spawn_id)
        assert row is not None

        reconciled = reconcile_active_spawn(tmp_path, runtime_root, row)

        assert reconciled.status == "failed"
        assert reconciled.terminal is not None
        assert reconciled.terminal.error == "crashed"
        backend.wait(timeout=5)
        tui.wait(timeout=5)
        assert backend.poll() is not None
        assert tui.poll() is not None
    finally:
        _stop(backend)
        _stop(tui)


def test_terminal_lockless_cleanup_claim_is_swept(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p1",
        chat_id="c1",
        model="test",
        agent="tester",
        harness="claude",
        kind="child",
        prompt="hello",
        started_at="2000-01-01T00:00:00Z",
        status="running",
    )
    sleeper, birth = _sleep_process()
    try:
        scope = ProcessScopeSnapshot(
            scope_id="harness",
            owner_policy="spawn_owned",
            owner_id="p1",
            role="harness_backend",
            containment="pid_tree_fallback",
            root_pid=sleeper.pid,
            root_created_at_epoch=birth,
            pgid=None,
            job_name=None,
            degraded_reason=None,
        )
        record_scope(launch_paths(runtime_root, SpawnId("p1")), scope)
        claim_active_spawn_scopes(runtime_root, SpawnId("p1"))
        spawn_store.finalize_spawn(
            runtime_root,
            "p1",
            "failed",
            1,
            origin="reconciler",
            error="crashed",
        )
        terminal = spawn_store.get_spawn(runtime_root, "p1")
        assert terminal is not None

        reconciled = reconcile_active_spawn(tmp_path, runtime_root, terminal)

        assert reconciled == terminal
        sleeper.wait(timeout=5)
        assert sleeper.poll() is not None
        assert not cleanup_claim_path(runtime_root, "p1").exists()
    finally:
        _stop(sleeper)


def test_lockless_crash_projection_is_never_finalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("[settings]\n", encoding="utf-8")
    (project_root / ".mars" / "agents").mkdir(parents=True)
    (project_root / ".mars" / "skills").mkdir(parents=True)
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    started_at = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    spawn_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p1",
        chat_id="c1",
        model="test",
        agent="tester",
        harness="claude",
        kind="child",
        prompt="hello",
        started_at=started_at,
        status="running",
    ).spawn_id
    state_path = runtime_root / "spawns" / str(spawn_id) / "state.json"
    before = state_path.read_bytes()

    shown = spawn_show_sync(
        SpawnShowInput(spawn_id=str(spawn_id), project_root=project_root.as_posix())
    )
    assert shown.status == "failed"
    assert shown.failure_reason == "crashed"

    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert reconcile_active_spawn(project_root, runtime_root, row) == row
    assert state_path.read_bytes() == before

    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )
    doctor_sync(DoctorInput(project_root=project_root.as_posix(), kill_orphans=True))

    assert state_path.read_bytes() == before
