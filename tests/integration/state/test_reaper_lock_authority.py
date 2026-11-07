"""Reaper writes are authorized by the runner-lifetime launch lock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_service import SpawnApplicationService
from meridian.lib.core.types import SpawnId
from meridian.lib.ops import diag
from meridian.lib.ops import mars as mars_ops
from meridian.lib.ops.diag import DoctorInput, doctor_sync
from meridian.lib.ops.spawn.api import spawn_show_sync
from meridian.lib.ops.spawn.models import SpawnShowInput
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.reaper import reconcile_active_spawn
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


@pytest.mark.asyncio
async def test_cancel_dead_runner_with_report_keeps_the_completion(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_id = _active_spawn(runtime_root)
    launch_paths(runtime_root, spawn_id).report.write_text(
        "# Finished\n\nCompleted before the runner crashed.\n",
        encoding="utf-8",
    )
    service = SpawnApplicationService(
        runtime_root,
        SpawnLifecycleService(runtime_root),
    )

    outcome = await service.cancel(SpawnId(spawn_id))

    assert outcome.status == "succeeded"
    persisted = spawn_store.get_spawn(runtime_root, spawn_id)
    assert persisted is not None
    assert persisted.status == "succeeded"
    assert persisted.terminal is not None
    assert persisted.terminal.exit_code == 0
    assert persisted.terminal.error is None


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
