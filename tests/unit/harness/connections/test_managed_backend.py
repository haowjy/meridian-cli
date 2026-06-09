"""Managed backend launch helper tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import managed_backend
from meridian.lib.harness.connections.managed_backend import (
    ManagedBackendConfig,
    launch_managed_backend,
)
from meridian.lib.platform.detached_process import DetachedSubprocessConfig, ParentDeathLink
from meridian.lib.platform.process_scope.base import PROCESS_BIRTH_UNKNOWN_EPOCH
from meridian.lib.state.paths import resolve_project_runtime_root, resolve_spawn_log_dir
from meridian.lib.state.process_scope_projection import read_scopes_from_disk


@pytest.mark.asyncio
async def test_launch_managed_backend_records_scope_with_parent_death_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("spawn-3")
    spawn_dir = resolve_spawn_log_dir(tmp_path, spawn_id)
    stderr_path = spawn_dir / "stderr.log"

    monkeypatch.setattr(
        managed_backend,
        "detached_subprocess_config",
        lambda: DetachedSubprocessConfig(kwargs={}, parent_death_linked=True),
    )
    monkeypatch.setattr(
        managed_backend,
        "link_child_lifetime_to_parent",
        lambda _pid: ParentDeathLink(parent_death_linked=False),
    )

    handle = await launch_managed_backend(
        ManagedBackendConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CODEX,
            command=(sys.executable, "-c", "pass"),
            cwd=tmp_path,
            env=os.environ.copy(),
            control_root=tmp_path,
            stderr_log_path=stderr_path,
            observer_mode=False,
        ),
        stderr=asyncio.subprocess.DEVNULL,
    )

    runtime_root = resolve_project_runtime_root(tmp_path)
    scopes = read_scopes_from_disk(runtime_root, spawn_id)

    assert handle.parent_death_linked is True
    assert handle.scope_snapshot.parent_death_linked is True
    assert handle.scope_snapshot.root_pid == handle.pid
    assert [(scope.scope_id, scope.root_pid, scope.parent_death_linked) for scope in scopes] == [
        ("backend", handle.pid, True)
    ]
    await handle.process.wait()


@pytest.mark.asyncio
async def test_launch_managed_backend_uses_windows_job_parent_death_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("spawn-win")
    job_handle: Any = object()

    monkeypatch.setattr(managed_backend, "IS_WINDOWS", True)
    monkeypatch.setattr(
        managed_backend,
        "detached_subprocess_config",
        lambda: DetachedSubprocessConfig(kwargs={}, parent_death_linked=False),
    )
    monkeypatch.setattr(
        managed_backend,
        "link_child_lifetime_to_parent",
        lambda _pid: ParentDeathLink(
            job_name="meridian-test-job",
            job_handle=job_handle,
            parent_death_linked=True,
        ),
    )

    handle = await launch_managed_backend(
        ManagedBackendConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.OPENCODE,
            command=(sys.executable, "-c", "pass"),
            cwd=tmp_path,
            env=os.environ.copy(),
            control_root=tmp_path,
            stderr_log_path=tmp_path / "stderr.log",
            observer_mode=True,
        ),
        stderr=asyncio.subprocess.DEVNULL,
    )

    assert handle.parent_death_linked is True
    assert handle.parent_death_link.job_handle is job_handle
    assert handle.scope_snapshot.parent_death_linked is True
    assert handle.scope_snapshot.containment == "windows_job"
    assert handle.scope_snapshot.job_name == "meridian-test-job"
    scopes = read_scopes_from_disk(resolve_project_runtime_root(tmp_path), spawn_id)
    assert [(scope.scope_id, scope.root_pid, scope.owner_policy) for scope in scopes] == [
        ("backend", handle.pid, "spawn_owned")
    ]
    await handle.process.wait()


@pytest.mark.asyncio
async def test_launch_managed_backend_records_unknown_birth_sentinel_when_create_time_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("spawn-unknown-birth")

    class _InaccessibleProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            raise psutil.AccessDenied(pid=self.pid)

    monkeypatch.setattr(managed_backend.psutil, "Process", _InaccessibleProcess)
    monkeypatch.setattr(
        managed_backend,
        "detached_subprocess_config",
        lambda: DetachedSubprocessConfig(kwargs={}, parent_death_linked=False),
    )
    monkeypatch.setattr(
        managed_backend,
        "link_child_lifetime_to_parent",
        lambda _pid: ParentDeathLink(parent_death_linked=False),
    )

    handle = await launch_managed_backend(
        ManagedBackendConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.OPENCODE,
            command=(sys.executable, "-c", "pass"),
            cwd=tmp_path,
            env=os.environ.copy(),
            control_root=tmp_path,
            stderr_log_path=tmp_path / "stderr.log",
            observer_mode=True,
        ),
        stderr=asyncio.subprocess.DEVNULL,
    )

    scopes = read_scopes_from_disk(resolve_project_runtime_root(tmp_path), spawn_id)
    assert handle.scope_snapshot.root_created_at_epoch == PROCESS_BIRTH_UNKNOWN_EPOCH
    assert [scope.root_created_at_epoch for scope in scopes] == [PROCESS_BIRTH_UNKNOWN_EPOCH]
    await handle.process.wait()
