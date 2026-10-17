# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false
# qa-validated: test-suite-redesign

"""Read-only reaper projection behavior for list/stats/wait surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tests.integration.state.conftest import (
    _OLD_STARTED_AT,
    _create_spawn,
    _get_spawn,
    _reconcile,
    recording_scope_cleanup,
)

if TYPE_CHECKING:
    import pytest


def test_read_projection_does_not_reap_recorded_scope_but_explicit_reconcile_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.core.types import SpawnId
    from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
    from meridian.lib.state.process_scope_projection import record_scope

    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="child",
        harness="opencode",
        runner_pid=9301,
        started_at=_OLD_STARTED_AT,
    )
    (runtime_root / "spawns" / spawn_id / "launch.lock").touch()
    backend_scope = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id=spawn_id,
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=9402,
        root_created_at_epoch=100.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )
    record_scope(runtime_root, SpawnId(spawn_id), backend_scope)
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    terminated_scopes = recording_scope_cleanup(
        monkeypatch,
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
    )

    from meridian.lib.state.reaper import reconcile_spawns
    from meridian.lib.state.spawn_store import SpawnScan

    projected = reconcile_spawns(
        tmp_path,
        runtime_root,
        SpawnScan(records=(record,), quarantines=()),
    )
    assert projected.records[0].status == "failed"
    assert projected.records[0].terminal is not None
    assert projected.records[0].terminal.error == "crashed"
    assert _get_spawn(runtime_root, spawn_id).status == "running"
    assert terminated_scopes == []

    assert _get_spawn(runtime_root, spawn_id).status == "running"
    assert terminated_scopes == []

    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.terminal is not None
    assert reconciled.terminal.error == "crashed"
    assert terminated_scopes == ["backend:9402:reaper"]
