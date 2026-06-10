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

from pathlib import Path
from typing import TYPE_CHECKING

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.ops.spawn.models import SpawnCancelInput
from tests.integration.state.conftest import (
    _OLD_STARTED_AT,
    _create_spawn,
    _get_spawn,
    _reconcile,
    _write_corrupt_primary_meta,
    _write_primary_meta,
    fake_managed_primary_birth_liveness,
    fake_reaper_liveness,
    recording_scope_cleanup,
)

if TYPE_CHECKING:
    import pytest


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
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def terminate(self) -> None:
            terminated_pids.append(self.pid)

    monkeypatch.setattr("meridian.lib.state.managed_primary.psutil.Process", _FakeProcess)

    reconciled = _reconcile(tmp_path, runtime_root, _get_spawn(runtime_root, spawn_id))

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
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
    assert latest.error == "orphan_primary"


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
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
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
    assert latest.error == "orphan_primary"


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
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def terminate(self) -> None:
            terminated_pids.append(self.pid)

    monkeypatch.setattr("meridian.lib.state.managed_primary.psutil.Process", _FakeProcess)

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "finalizing"
    assert output.exit_code == 1
    assert terminated_pids[0] == 7001
    assert set(terminated_pids[1:]) == {7002, 7003}
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "finalizing"
    assert latest.cancel_intent is not None
    assert latest.cancel_intent.exit_code == 130
    assert latest.cancel_intent.error == "cancelled"


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
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def terminate(self) -> None:
            terminated_pids.append(self.pid)

    monkeypatch.setattr("meridian.lib.state.managed_primary.psutil.Process", _FakeProcess)

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
    assert latest.error == "cancelled"
    assert latest.terminal_origin == "cancel"
