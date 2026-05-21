from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import meridian.lib.ops.spawn.api as spawn_api
import meridian.lib.ops.spawn.execute as spawn_execute
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.ops.runtime import resolve_runtime_authority_for_read
from meridian.lib.ops.spawn.execute_bg import _load_bg_worker_request
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_spawn_log_dir
from tests.support.launch import stub_bundle_request_and_resolve


def _seed_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "repo"
    task_cwd = project_root / "tests" / "smoke"
    (project_root / ".git").mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    task_cwd.mkdir(parents=True, exist_ok=True)
    return project_root, task_cwd


def test_spawn_create_background_persists_distinct_task_cwd(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    project_root, task_cwd = _seed_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.chdir(task_cwd)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX,
    )

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        spawn_execute,
        "_build_background_worker_command",
        lambda **_kwargs: (sys.executable, "-c", "pass"),
    )
    monkeypatch.setattr(
        spawn_execute,
        "_record_launch_boundary_observation",
        lambda runtime_root, spawn_id, **kwargs: captured.setdefault("launch_cwds", []).append(
            kwargs.get("cwd")
        ),
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="reply with OK",
            model="gpt-5.3-codex",
            harness="codex",
            background=True,
            project_root=project_root.as_posix(),
        )
    )

    assert result.status == "running"
    assert task_cwd.as_posix() in captured["launch_cwds"]
    assert result.spawn_id is not None

    authority = resolve_runtime_authority_for_read(project_root)
    assert authority.runtime_root is not None
    spawn_id = SpawnId(result.spawn_id)
    row = spawn_store.get_spawn(authority.runtime_root, spawn_id)

    assert row is not None
    assert row.control_root == project_root.as_posix()
    assert row.task_cwd == task_cwd.as_posix()
    assert row.execution_cwd == task_cwd.as_posix()

    bg_request = _load_bg_worker_request(resolve_spawn_log_dir(project_root, spawn_id))
    assert bg_request.runtime.control_root == project_root.as_posix()
    assert bg_request.runtime.requested_task_cwd == task_cwd.as_posix()
    assert bg_request.runtime.project_paths_execution_cwd == task_cwd.as_posix()


def test_spawn_create_blocking_passes_distinct_task_cwd_to_launch_runtime(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    project_root, task_cwd = _seed_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.chdir(task_cwd)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX,
    )

    captured: dict[str, Any] = {}

    async def _fake_launch_prepared_spawn(
        *,
        runtime_request: Any,
        execution_cwd: str,
        **_kwargs: object,
    ) -> int:
        captured["runtime_requested_task_cwd"] = runtime_request.requested_task_cwd
        captured["runtime_control_root"] = runtime_request.control_root
        captured["execution_cwd"] = execution_cwd
        return 0

    monkeypatch.setattr(spawn_execute, "launch_prepared_spawn", _fake_launch_prepared_spawn)
    monkeypatch.setattr(
        spawn_execute,
        "read_spawn_row",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="succeeded",
            duration_secs=0.0,
            input_tokens=None,
            output_tokens=None,
        ),
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="reply with OK",
            model="gpt-5.3-codex",
            harness="codex",
            project_root=project_root.as_posix(),
        )
    )

    assert result.status == "succeeded"
    assert captured["runtime_control_root"] == project_root.as_posix()
    assert captured["runtime_requested_task_cwd"] == task_cwd.as_posix()
    assert captured["execution_cwd"] == task_cwd.as_posix()

    assert result.spawn_id is not None
    authority = resolve_runtime_authority_for_read(project_root)
    assert authority.runtime_root is not None
    row = spawn_store.get_spawn(authority.runtime_root, SpawnId(result.spawn_id))

    assert row is not None
    assert row.control_root == project_root.as_posix()
    assert row.task_cwd == task_cwd.as_posix()
    assert row.execution_cwd == task_cwd.as_posix()
