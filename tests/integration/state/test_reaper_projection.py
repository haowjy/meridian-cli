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
    fake_reaper_liveness,
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
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    fake_reaper_liveness(monkeypatch, set())
    terminated_scopes = recording_scope_cleanup(
        monkeypatch,
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
    )

    from meridian.lib.state.reaper import reconcile_spawns

    projected = reconcile_spawns(tmp_path, runtime_root, [record])
    assert projected[0].status == "failed"
    assert projected[0].error == "orphan_run"
    assert _get_spawn(runtime_root, spawn_id).status == "running"
    assert terminated_scopes == []

    from meridian.lib.ops.spawn import api as spawn_api
    from meridian.lib.ops.spawn.models import SpawnListInput, SpawnStatsInput, SpawnWaitInput

    listed = spawn_api.spawn_list_sync(
        SpawnListInput(statuses=(), project_root=tmp_path.as_posix())
    )
    assert listed.spawns[0].spawn_id == spawn_id
    assert listed.spawns[0].status == "failed"
    stats = spawn_api.spawn_stats_sync(SpawnStatsInput(project_root=tmp_path.as_posix()))
    assert stats.failed == 1
    waited = spawn_api.spawn_wait_sync(
        SpawnWaitInput(
            spawn_ids=(spawn_id,),
            timeout=0.01,
            timeout_explicit=True,
            poll_interval_secs=0.01,
            project_root=tmp_path.as_posix(),
        )
    )
    assert waited.spawns[0].status == "failed"
    assert _get_spawn(runtime_root, spawn_id).status == "running"
    assert terminated_scopes == []

    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.error == "orphan_run"
    assert terminated_scopes == ["backend:9402:reaper"]
