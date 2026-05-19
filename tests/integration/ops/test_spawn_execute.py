# qa-validated: pi-rpc-quiescence
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.pi import PiAdapter
from meridian.lib.harness.pi_runtime_resolver import PiRuntimeResolution
from meridian.lib.launch.context import LaunchContext
from meridian.lib.launch.launch_types import (
    ResolvedLaunchBinding,
    ResolvedLaunchEnvironment,
    ResolvedLaunchSpec,
)
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.ops.spawn.execute import launch_prepared_spawn
from meridian.lib.ops.spawn.execute_runner import PreparedExecutionHandoff
from meridian.lib.ops.spawn.execute_session import _SessionExecutionContext
from meridian.lib.safety.permissions import PermissionConfig, UnsafeNoOpPermissionResolver
from meridian.lib.state.paths import resolve_spawn_log_dir


@pytest.mark.asyncio
async def test_launch_prepared_spawn_closes_session_scope_when_runner_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute_runner as execute_module

    close_count = 0

    class _CountingExitStack:
        def close(self) -> None:
            nonlocal close_count
            close_count += 1

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return PreparedExecutionHandoff(
            resolved_request=SpawnRequest(
                prompt="run it",
                model="gpt-5.4",
                harness="codex",
                agent="coder",
            ),
            launch_context=cast(
                "Any",
                SimpleNamespace(
                    harness=SimpleNamespace(id=HarnessId.OPENCODE),
                ),
            ),
            session_context=_SessionExecutionContext(
                chat_id="c1",
                work_id=None,
                resolved_agent_name="coder",
                harness_session_id_observer=lambda _session_id: None,
            ),
            session_exit_stack=cast("Any", _CountingExitStack()),
            execution_cwd=tmp_path.as_posix(),
            work_id=None,
            harness_session_id_observer=lambda _session_id: None,
        )

    async def fake_invoke_runner(
        _handoff: PreparedExecutionHandoff,
        **_kwargs: object,
    ) -> int:
        raise RuntimeError("runner boom")

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)

    with pytest.raises(RuntimeError, match="runner boom"):
        await launch_prepared_spawn(
            spawn=Spawn(
                spawn_id=SpawnId("p1"),
                prompt="run it",
                model=ModelId("gpt-5.4"),
                status="queued",
            ),
            request=SpawnRequest(
                prompt="run it",
                model="gpt-5.4",
                harness="codex",
                agent="coder",
            ),
            runtime_request=LaunchRuntime(
                runtime_root=(tmp_path / ".runtime").as_posix(),
                project_paths_project_root=tmp_path.as_posix(),
                project_paths_execution_cwd=tmp_path.as_posix(),
            ),
            runtime=cast(
                "Any",
                SimpleNamespace(
                    harness_registry=SimpleNamespace(),
                    artifacts=None,
                ),
            ),
            runtime_root=tmp_path / ".runtime",
            project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
            execution_cwd=tmp_path.as_posix(),
        )

    assert close_count == 1


@pytest.mark.asyncio
async def test_launch_prepared_spawn_persists_pi_runtime_metadata_with_scoped_session_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute_runner as execute_module

    spawn_id = SpawnId("p-pi-runtime-meta")

    runtime_root = tmp_path / ".runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    request = SpawnRequest(
        prompt="run it",
        model="gpt-5.4-mini",
        harness="pi",
        agent="coder",
    )
    runtime_request = LaunchRuntime(
        runtime_root=runtime_root.as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=tmp_path.as_posix(),
    )
    permission_resolver = UnsafeNoOpPermissionResolver(_suppress_warning=True)
    base_session_dir = tmp_path / "pi-sessions"
    final_env = {
        "MERIDIAN_PI_SESSION_ROLE": "spawned",
        "PI_CODING_AGENT_SESSION_DIR": str(base_session_dir),
        "PATH": os.environ.get("PATH", ""),
    }
    launch_context = LaunchContext(
        request=request,
        runtime=runtime_request,
        project_root=tmp_path,
        execution_cwd=tmp_path,
        control_root=tmp_path,
        task_cwd=None,
        runtime_root=runtime_root,
        work_id=None,
        binding=ResolvedLaunchBinding(
            work_id=None,
            child_cwd=tmp_path,
            report_output_path=tmp_path / "report.md",
            run_params=SpawnParams(prompt="run it"),
            permission_config=PermissionConfig(),
            perms=permission_resolver,
            spec=ResolvedLaunchSpec(
                harness=HarnessId.PI,
                prompt="run it",
                permission_resolver=permission_resolver,
            ),
            argv=("pi", "--mode", "rpc"),
            environment=ResolvedLaunchEnvironment.build(
                child_context_env={},
                plan_env={},
                preflight_env={},
                workspace_env={},
                runtime_override_env={},
                bind_env_overrides={},
                runner_overlay_env={},
                final_env=final_env,
            ),
        ),
        harness=PiAdapter(),
        resolved_request=request,
    )

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return PreparedExecutionHandoff(
            resolved_request=request,
            launch_context=launch_context,
            session_context=_SessionExecutionContext(
                chat_id="c-pi-runtime-meta",
                work_id=None,
                resolved_agent_name="coder",
                harness_session_id_observer=lambda _session_id: None,
            ),
            session_exit_stack=cast("Any", SimpleNamespace(close=lambda: None)),
            execution_cwd=tmp_path.as_posix(),
            work_id=None,
            harness_session_id_observer=lambda _session_id: None,
        )

    captured_env: dict[str, str] = {}

    async def fake_invoke_runner(
        handoff: PreparedExecutionHandoff,
        **_kwargs: object,
    ) -> int:
        captured_env.update(dict(handoff.launch_context.binding.environment.final_env))
        return 0

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 1.2.3",
        ),
    )

    exit_code = await launch_prepared_spawn(
        spawn=Spawn(
            spawn_id=spawn_id,
            prompt="run it",
            model=ModelId("gpt-5.4-mini"),
            status="queued",
        ),
        request=request,
        runtime_request=runtime_request,
        runtime=cast(
            "Any",
            SimpleNamespace(
                harness_registry=SimpleNamespace(),
                artifacts=None,
            ),
        ),
        runtime_root=runtime_root,
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )
    assert exit_code == 0

    expected_scoped_session_dir = str(base_session_dir / str(spawn_id))
    assert captured_env["MERIDIAN_PI_BINARY"] == "/usr/local/bin/pi"
    assert captured_env["MERIDIAN_PI_LIFECYCLE_EVENT_FILE"].endswith("pi-lifecycle-events.jsonl")
    assert captured_env["PI_CODING_AGENT_SESSION_DIR"] == expected_scoped_session_dir

    metadata_path = resolve_spawn_log_dir(tmp_path, spawn_id) / "pi_runtime_meta.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["runtime_kind"] == "path"
    assert payload["runtime_path"] == "/usr/local/bin/pi"
    assert payload["runtime_version"] == "pi 1.2.3"
    assert payload["session_dir"] == expected_scoped_session_dir
    assert payload["auth_policy"] == "inherit-runtime-default-auth-config"
