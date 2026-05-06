from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.domain import Spawn
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.ops.spawn.execute import (
    PreparedExecutionHandoff,
    _SessionExecutionContext,
    launch_prepared_spawn,
)
from meridian.lib.state import spawn_store


def _start_spawn(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: str = "p1",
    status: str = "running",
) -> SpawnId:
    service = create_lifecycle_service(project_root, runtime_root)
    return SpawnId(
        service.start(
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            skills=(),
            skill_paths=(),
            harness="codex",
            kind="child",
            prompt="run it",
            spawn_id=spawn_id,
            status=cast("Any", status),
        )
    )


def _runtime_request(project_root: Path, runtime_root: Path) -> LaunchRuntime:
    return LaunchRuntime(
        runtime_root=runtime_root.as_posix(),
        project_paths_project_root=project_root.as_posix(),
        project_paths_execution_cwd=project_root.as_posix(),
    )


class _HarnessRegistry:
    def get_subprocess_harness(self, _harness_id: object) -> SimpleNamespace:
        return SimpleNamespace(
            capabilities=SimpleNamespace(
                supports_session_resume=False,
                supports_session_fork=False,
            )
        )


def _runtime(project_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=project_root,
        harness_registry=_HarnessRegistry(),
        artifacts=None,
    )


def _spawn(spawn_id: SpawnId, *, status: str = "running") -> Spawn:
    return Spawn(
        spawn_id=spawn_id,
        prompt="run it",
        model=ModelId("gpt-5.4"),
        status=cast("Any", status),
    )


@pytest.mark.asyncio
async def test_launch_prepared_spawn_keeps_runner_terminal_tuple_when_teardown_close_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    runtime_root = tmp_path / ".runtime"
    spawn_id = _start_spawn(project_root=tmp_path, runtime_root=runtime_root, status="queued")
    warning_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _ExplodingExitStack:
        def close(self) -> None:
            raise RuntimeError("teardown boom")

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
                SimpleNamespace(harness=SimpleNamespace(id=HarnessId.CODEX)),
            ),
            session_context=_SessionExecutionContext(
                chat_id="c1",
                work_id=None,
                resolved_agent_name="coder",
                harness_session_id_observer=lambda _session_id: None,
            ),
            session_exit_stack=cast("Any", _ExplodingExitStack()),
            execution_cwd=tmp_path.as_posix(),
            work_id=None,
            harness_session_id_observer=lambda _session_id: None,
        )

    async def fake_invoke_runner(*args: object, **kwargs: object) -> int:
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            status="cancelled",
            exit_code=143,
            origin="runner",
            error="cancelled",
        )
        return 143

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)

    def capture_warning(*args: object, **kwargs: object) -> None:
        warning_calls.append((args, kwargs))

    monkeypatch.setattr(execute_module.logger, "warning", capture_warning)

    result = await launch_prepared_spawn(
        spawn=_spawn(spawn_id, status="queued"),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex", agent="coder"),
        runtime_request=_runtime_request(tmp_path, runtime_root),
        runtime=cast("Any", _runtime(tmp_path)),
        runtime_root=runtime_root,
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert result == 143
    assert row is not None
    assert row.status == "cancelled"
    assert row.exit_code == 143
    assert row.terminal_origin == "runner"
    assert row.error == "cancelled"
    assert len(warning_calls) == 1
    assert warning_calls[0][0][0] == "Post-run session teardown failed."
