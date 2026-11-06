# qa-validated: pi-rpc-quiescence
"""spawn stats, list, show, cancel_all, and wait checkpoint behaviors.

spawn_create_sync tests live in test_spawn_api_create.py.

# qa-validated: test-suite-redesign
"""

import json
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.context import RuntimeContext
from meridian.lib.ops.spawn.models import (
    SpawnWaitInput,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.support.launch_paths import launch_paths


def _state_root(project_root: Path) -> Path:
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


@pytest.mark.parametrize("observe", [True, False])
def test_wait_can_fetch_results_without_consuming_parent_notifications(
    tmp_path: Path, observe: bool
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    spawn_id = spawn_store.start_spawn(
        runtime_root,
        chat_id="c-wait-observation",
        model="test",
        agent="coder",
        harness="pi",
        prompt="finished",
    ).spawn_id
    spawn_store.finalize_spawn(runtime_root, spawn_id, "succeeded", 0, origin="runner")
    observer_id = spawn_store.start_spawn(
        runtime_root,
        chat_id="c-wait-observation",
        model="test",
        agent="coder",
        harness="pi",
        prompt="observer",
    ).spawn_id
    result = spawn_api.spawn_wait_sync(
        SpawnWaitInput(project_root=str(project_root), spawn_ids=(str(spawn_id),), observe=observe),
        ctx=RuntimeContext(spawn_id=observer_id),
    )
    assert result.total_runs == 1
    assert result.spawns[0].status == "succeeded"
    observed_path = runtime_root / "spawns" / str(observer_id) / "observed-launches.json"
    if observe:
        observed = json.loads(observed_path.read_text())
        assert observed["observed_launches"] == [f"{spawn_id}:1"]
        assert observed["waiting_launches"] == []
        assert observed["wait_pins"] == {}
    else:
        assert not observed_path.exists()


def _write_primary_meta(
    runtime_root: Path,
    spawn_id: str,
    *,
    activity: str,
    backend_pid: int | None = None,
    tui_pid: int | None = None,
) -> None:
    meta_path = launch_paths(runtime_root, spawn_id).primary_meta
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "managed_backend": True,
                "activity": activity,
                "backend_pid": backend_pid,
                "tui_pid": tui_pid,
            }
        )
        + "\n",
        encoding="utf-8",
    )
