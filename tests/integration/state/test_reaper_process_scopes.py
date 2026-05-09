from __future__ import annotations

from pathlib import Path

import pytest  # noqa: TC002

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.process_scope_projection import is_scope_released, record_scope
from meridian.lib.state.reaper import reconcile_active_spawn
from meridian.lib.state.spawn.model import SpawnRecord

_OLD_STARTED_AT = "2000-01-01T00:00:00Z"


def _runtime_root(tmp_path: Path) -> Path:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _create_spawn(
    tmp_path: Path,
    *,
    spawn_id: str = "p1",
    worker_pid: int | None = None,
    runner_pid: int | None = 123,
) -> tuple[Path, str]:
    runtime_root = _runtime_root(tmp_path)
    created = spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.4",
        agent="tester",
        harness="codex",
        kind="child",
        prompt="hello",
        worker_pid=worker_pid,
        runner_pid=runner_pid,
        started_at=_OLD_STARTED_AT,
        status="running",
    )
    return runtime_root, str(created)


def _get_spawn(runtime_root: Path, spawn_id: str) -> SpawnRecord:
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert record is not None
    return record


def test_reaper_uses_persisted_scope_metadata_instead_of_worker_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, worker_pid=222, runner_pid=111)
    record_scope(
        runtime_root,
        SpawnId(spawn_id),
        ProcessScopeSnapshot(
            scope_id="backend",
            owner_policy="spawn_owned",
            owner_id=spawn_id,
            role="harness_backend",
            containment="pid_tree_fallback",
            root_pid=333,
            root_created_at_epoch=100.0,
            pgid=None,
            job_name=None,
            degraded_reason=None,
        ),
    )

    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    terminated: list[int] = []

    def _fake_terminate_tree_sync(
        pid: int,
        *,
        created_at_epoch: float = 0.0,
        grace_secs: float = 5.0,
        reason: str = "stop_called",
        scope_id: str = "",
        degraded_fallback: bool = False,
    ) -> CleanupResult:
        terminated.append(pid)
        return CleanupResult(
            scope_id=scope_id,
            root_pid=pid,
            descendant_count=1,
            reason=reason,
            grace_seconds=grace_secs,
            kill_escalated=False,
            degraded_fallback=degraded_fallback,
            skip_reason=None,
        )

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )

    reconciled = reconcile_active_spawn(tmp_path, runtime_root, _get_spawn(runtime_root, spawn_id))

    assert terminated == [333]
    assert reconciled.status == "failed"
    assert reconciled.error == "orphan_run"
    assert is_scope_released(runtime_root, SpawnId(spawn_id), "backend") is True
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_run"
