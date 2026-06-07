"""Backend lifecycle sidecar schema tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.managed_backend import (
    BackendPhase,
    ManagedBackend,
    ManagedBackendConfig,
)
from meridian.lib.launch.constants import BACKEND_LIFECYCLE_FILENAME
from meridian.lib.platform.process_scope import ProcessScopeSnapshot
from meridian.lib.state.backend_lifecycle import (
    BackendLifecycleRecord,
    backend_lifecycle_path,
    read_backend_lifecycle,
    write_backend_lifecycle,
)
from meridian.lib.state.paths import resolve_project_runtime_root, resolve_spawn_log_dir


def _sample_snapshot() -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id="spawn-1",
        role="harness_backend",
        containment="posix_pgid",
        root_pid=4242,
        root_created_at_epoch=1717700000.0,
        pgid=4242,
        job_name=None,
        degraded_reason=None,
    )


def test_backend_lifecycle_round_trip(tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_dir = runtime_root / "spawns" / "spawn-1"
    spawn_dir.mkdir(parents=True)

    record = BackendLifecycleRecord(
        phase=BackendPhase.LAUNCHING.value,
        phase_entered_epoch=1717700000.0,
        phase_timeout_seconds=30.0,
        backend_pid=4242,
        backend_birth_epoch=1717699990.0,
        scope_snapshot=_sample_snapshot(),
        harness_session_id=None,
        parent_death_linked=True,
    )
    write_backend_lifecycle(spawn_dir, record)

    lifecycle_path = backend_lifecycle_path(spawn_dir=spawn_dir)
    assert lifecycle_path.name == BACKEND_LIFECYCLE_FILENAME
    assert lifecycle_path.is_file()

    loaded = read_backend_lifecycle(runtime_root, "spawn-1")
    assert loaded is not None
    assert loaded.phase == "launching"
    assert loaded.phase_entered_epoch == 1717700000.0
    assert loaded.phase_timeout_seconds == 30.0
    assert loaded.backend_pid == 4242
    assert loaded.backend_birth_epoch == 1717699990.0
    assert loaded.harness_session_id is None
    assert loaded.parent_death_linked is True
    assert loaded.scope_snapshot.scope_id == "backend"
    assert loaded.scope_snapshot.root_pid == 4242


def test_read_backend_lifecycle_missing_or_corrupt_returns_none(tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_dir = runtime_root / "spawns" / "spawn-2"
    spawn_dir.mkdir(parents=True)

    assert read_backend_lifecycle(runtime_root, "spawn-2") is None

    corrupt_path = backend_lifecycle_path(spawn_dir=spawn_dir)
    corrupt_path.write_text("{not-json", encoding="utf-8")
    assert read_backend_lifecycle(runtime_root, "spawn-2") is None

    corrupt_path.write_text(json.dumps({"phase": "launching"}), encoding="utf-8")
    assert read_backend_lifecycle(runtime_root, "spawn-2") is None


@pytest.mark.asyncio
async def test_managed_backend_launch_persists_lifecycle_sidecar(tmp_path) -> None:
    spawn_id = SpawnId("spawn-3")
    project_root = tmp_path
    spawn_dir = resolve_spawn_log_dir(project_root, spawn_id)
    spawn_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = spawn_dir / "stderr.log"

    backend = ManagedBackend(spawn_dir=spawn_dir)
    handle = await backend.launch(
        ManagedBackendConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CODEX,
            command=(sys.executable, "-c", "pass"),
            cwd=project_root,
            env=os.environ.copy(),
            control_root=project_root,
            stderr_log_path=stderr_path,
            observer_mode=True,
        ),
        stderr=asyncio.subprocess.DEVNULL,
    )

    runtime_root = resolve_project_runtime_root(project_root)
    loaded = read_backend_lifecycle(runtime_root, str(spawn_id))
    assert loaded is not None
    assert loaded.phase == BackendPhase.LAUNCHING.value
    assert loaded.backend_pid == handle.pid
    assert loaded.scope_snapshot.root_pid == handle.pid
    assert loaded.harness_session_id is None
