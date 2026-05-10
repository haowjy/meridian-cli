from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.ops.spawn.execute import launch_prepared_spawn
from meridian.lib.ops.spawn.execute_runner import PreparedExecutionHandoff
from meridian.lib.ops.spawn.execute_session import _SessionExecutionContext


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
