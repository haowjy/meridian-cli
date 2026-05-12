"""Shared helpers for state integration tests.

Reaper helpers are used across test_reaper_reconciliation.py,
test_reaper_managed_primary.py, and test_reaper_cancel.py.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.launch.constants import PRIMARY_META_FILENAME
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_boundary import record_launch_boundary_event
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.reaper import reconcile_active_spawn
from meridian.lib.state.spawn.model import LaunchMode, SpawnRecord

_OLD_STARTED_AT = "2000-01-01T00:00:00Z"


def _recent_started_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _state_root(tmp_path: Path) -> Path:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _create_spawn(
    tmp_path: Path,
    *,
    spawn_id: str = "p1",
    status: SpawnStatus = "running",
    kind: str = "child",
    harness: str = "codex",
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = 123,
    started_at: str | None = _OLD_STARTED_AT,
) -> tuple[Path, str]:
    runtime_root = _state_root(tmp_path)
    created_spawn_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.4",
        agent="tester",
        harness=harness,
        kind=kind,
        prompt="hello",
        worker_pid=worker_pid,
        launch_mode=launch_mode,
        status=status,
        runner_pid=runner_pid,
        started_at=started_at,
    )
    return runtime_root, str(created_spawn_id)


def _get_spawn(runtime_root: Path, spawn_id: str) -> SpawnRecord:
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert record is not None
    return record


def _reconcile(project_root: Path, runtime_root: Path, record: SpawnRecord) -> SpawnRecord:
    return reconcile_active_spawn(project_root, runtime_root, record)


def _write_report(
    runtime_root: Path,
    spawn_id: str,
    text: str = "# Finished\n\nCompleted.\n",
) -> Path:
    report_path = runtime_root / "spawns" / spawn_id / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding="utf-8")
    return report_path


def _write_primary_meta(
    runtime_root: Path,
    spawn_id: str,
    *,
    launcher_pid: int | None,
    backend_pid: int | None = None,
    tui_pid: int | None = None,
    activity: str = "idle",
    managed_backend: bool = True,
) -> Path:
    metadata_path = runtime_root / "spawns" / spawn_id / PRIMARY_META_FILENAME
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "managed_backend": managed_backend,
                "launcher_pid": launcher_pid,
                "backend_pid": backend_pid,
                "tui_pid": tui_pid,
                "activity": activity,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return metadata_path


def _write_corrupt_primary_meta(runtime_root: Path, spawn_id: str) -> Path:
    metadata_path = runtime_root / "spawns" / spawn_id / PRIMARY_META_FILENAME
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text("{corrupt json", encoding="utf-8")
    return metadata_path


def _set_artifact_age_secs(path: Path, *, age_secs: float) -> None:
    target_epoch = time.time() - age_secs
    os.utime(path, (target_epoch, target_epoch))


def _write_activity_artifact(
    runtime_root: Path,
    spawn_id: str,
    artifact_name: str,
    *,
    age_secs: float,
) -> Path:
    artifact_path = runtime_root / "spawns" / spawn_id / artifact_name
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if artifact_name == "heartbeat":
        artifact_path.touch()
    else:
        artifact_path.write_text("recent activity\n", encoding="utf-8")
    _set_artifact_age_secs(artifact_path, age_secs=age_secs)
    return artifact_path


def _record_launch_boundary(
    runtime_root: Path,
    spawn_id: str,
    *,
    event: str,
    launcher_pid: int | None = None,
    worker_pid: int | None = None,
) -> None:
    record_launch_boundary_event(
        runtime_root,
        spawn_id,
        event=event,
        launcher_pid=launcher_pid,
        worker_pid=worker_pid,
    )
