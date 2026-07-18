from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import meridian.lib.ops.spawn.api as spawn_api
import meridian.lib.ops.spawn.execute as spawn_execute
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.launch.request import SpawnRequest
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


def test_spawn_create_background_handoff_preserves_external_task_cwd(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    project_root, invocation_cwd = _seed_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    external_task_cwd = tmp_path / "outside-worktree"
    external_task_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.chdir(invocation_cwd)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX,
    )

    prepared_request = SpawnRequest(
        prompt="reply with OK",
        prompt_is_composed=True,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX.value,
        authority_root=project_root.as_posix(),
        task_cwd=external_task_cwd.as_posix(),
        reference_anchor=external_task_cwd.as_posix(),
        task_cwd_source="ambient-work-worktree",
        task_cwd_work_item="ambient-work",
    )
    monkeypatch.setattr(
        spawn_api,
        "build_create_payload",
        lambda *args, **kwargs: SimpleNamespace(request=prepared_request, prepared=None),
    )
    monkeypatch.setattr(
        spawn_execute,
        "_build_background_worker_command",
        lambda **_kwargs: (sys.executable, "-c", "pass"),
    )

    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            captured["cwd"] = kwargs.get("cwd")
            self.pid = 43210

    monkeypatch.setattr(spawn_execute.subprocess, "Popen", _FakePopen)

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
    assert captured["cwd"] == project_root.as_posix()
    assert result.spawn_id is not None

    authority = resolve_runtime_authority_for_read(project_root)
    assert authority.runtime_root is not None
    spawn_id = SpawnId(result.spawn_id)
    row = spawn_store.get_spawn(authority.runtime_root, spawn_id)

    assert row is not None
    assert row.control_root == project_root.as_posix()
    assert row.task_cwd == external_task_cwd.as_posix()

    bg_request = _load_bg_worker_request(resolve_spawn_log_dir(project_root, spawn_id))
    assert bg_request.runtime.control_root == project_root.as_posix()
    assert bg_request.runtime.requested_task_cwd == external_task_cwd.as_posix()


def test_spawn_create_persists_and_executes_prepared_task_cwd_contract(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    project_root, invocation_cwd = _seed_project(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    external_task_cwd = tmp_path / "outside-worktree"
    external_task_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.chdir(invocation_cwd)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX,
    )

    prepared_request = SpawnRequest(
        prompt="reply with OK",
        prompt_is_composed=True,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX.value,
        authority_root=project_root.as_posix(),
        task_cwd=external_task_cwd.as_posix(),
        reference_anchor=external_task_cwd.as_posix(),
        task_cwd_source="ambient-work-worktree",
        task_cwd_work_item="ambient-work",
    )
    monkeypatch.setattr(
        spawn_api,
        "build_create_payload",
        lambda *args, **kwargs: SimpleNamespace(request=prepared_request, prepared=None),
    )

    captured: dict[str, Any] = {}

    async def _fake_launch_prepared_spawn(
        *,
        runtime_request: Any,
        execution_cwd: str,
        work_id: str | None,
        **_kwargs: object,
    ) -> int:
        captured["runtime_requested_task_cwd"] = runtime_request.requested_task_cwd
        captured["runtime_control_root"] = runtime_request.control_root
        captured["execution_cwd"] = execution_cwd
        captured["work_id"] = work_id
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
    assert captured == {
        "runtime_control_root": project_root.as_posix(),
        "runtime_requested_task_cwd": external_task_cwd.as_posix(),
        "execution_cwd": external_task_cwd.as_posix(),
        "work_id": "ambient-work",
    }

    assert result.spawn_id is not None
    authority = resolve_runtime_authority_for_read(project_root)
    assert authority.runtime_root is not None
    row = spawn_store.get_spawn(authority.runtime_root, SpawnId(result.spawn_id))

    assert row is not None
    assert row.control_root == project_root.as_posix()
    assert row.task_cwd == external_task_cwd.as_posix()
    assert row.work_id == "ambient-work"
