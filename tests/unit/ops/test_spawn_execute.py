from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace as dataclass_replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.claude_preflight import MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV
from meridian.lib.harness.launch_spec import OpenCodeLaunchSpec
from meridian.lib.harness.opencode import OpenCodeAdapter
from meridian.lib.launch.artifact_io import write_projection_artifacts
from meridian.lib.launch.composition import (
    ProjectedContent,
    ProjectionChannels,
    ReferenceRouting,
)
from meridian.lib.launch.context import LaunchContext
from meridian.lib.launch.reference import ReferenceItem
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.run_inputs import ResolvedRunInputs
from meridian.lib.ops.spawn.execute import (
    BackgroundWorkerLaunchRequest,
    PreparedExecutionHandoff,
    _execute_existing_spawn,
    _prepare_execution_handoff,
    _SessionExecutionContext,
    _write_params_json,
    execute_spawn_blocking,
    launch_prepared_spawn,
)
from meridian.lib.ops.spawn.failure_policy import (
    finalize_launch_failure,
    finalize_launch_failure_sync,
)
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver
from meridian.lib.state import spawn_store


def _resolver() -> TieredPermissionResolver:
    return TieredPermissionResolver(config=PermissionConfig())


def _make_launch_context(
    *,
    tmp_path: Path,
    spec: OpenCodeLaunchSpec,
    run_inputs: ResolvedRunInputs,
    projected: ProjectedContent | None = None,
) -> LaunchContext:
    request = SpawnRequest(prompt=run_inputs.prompt, model="gpt-5.4", harness="opencode")
    runtime = LaunchRuntime(
        runtime_root=(tmp_path / ".meridian").as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=tmp_path.as_posix(),
    )
    return LaunchContext(
        request=request,
        runtime=runtime,
        project_root=tmp_path,
        execution_cwd=tmp_path,
        runtime_root=tmp_path / ".meridian",
        work_id=None,
        argv=("opencode", "run", "-"),
        run_params=run_inputs,
        perms=_resolver(),
        spec=spec,
        child_cwd=tmp_path,
        env=MappingProxyType({}),
        env_overrides=MappingProxyType({}),
        report_output_path=tmp_path / "report.md",
        harness=OpenCodeAdapter(),
        resolved_request=request,
        projected_content=projected,
    )


def test_write_projection_artifacts_uses_projected_content_for_spawn(tmp_path: Path) -> None:
    file_ref = ReferenceItem(
        kind="file",
        path=tmp_path / "src" / "auth.py",
        body="print('ok')",
    )
    directory_ref = ReferenceItem(
        kind="directory",
        path=tmp_path / "src",
        body="tree",
    )
    warning_file_ref = ReferenceItem(
        kind="file",
        path=tmp_path / "src" / "binary.dat",
        body="",
        warning="Binary file: 10KB",
    )
    reference_items = (file_ref, directory_ref, warning_file_ref)
    run_inputs = ResolvedRunInputs(
        prompt="do thing",
        model=ModelId("opencode-gpt-5.4"),
        project_root=tmp_path.as_posix(),
        reference_items=reference_items,
    )
    spec = OpenCodeLaunchSpec(
        prompt="do thing",
        permission_resolver=_resolver(),
    )
    projected = ProjectedContent(
        system_prompt="",
        user_turn_content="projected spawn",
        reference_routing=(
            ReferenceRouting(
                path=file_ref.path.as_posix(),
                type="file",
                routing="native-injection",
                native_flag=f"--file {file_ref.path.as_posix()}",
            ),
        ),
        channels=ProjectionChannels(
            system_instruction="inline",
            user_task_prompt="inline",
            task_context="native-injection",
        ),
    )
    launch_context = _make_launch_context(
        tmp_path=tmp_path,
        spec=spec,
        run_inputs=run_inputs,
        projected=projected,
    )
    log_dir = tmp_path / "spawn"
    log_dir.mkdir(parents=True)

    write_projection_artifacts(log_dir=log_dir, launch_context=launch_context, surface="spawn")

    assert not (log_dir / "prompt.md").exists()
    references_payload = json.loads((log_dir / "references.json").read_text(encoding="utf-8"))
    assert references_payload == [
        {
            "path": file_ref.path.as_posix(),
            "type": "file",
            "routing": "native-injection",
            "native_flag": f"--file {file_ref.path.as_posix()}",
        },
    ]
    assert json.loads((log_dir / "projection-manifest.json").read_text(encoding="utf-8")) == {
        "harness": "opencode",
        "surface": "spawn",
        "channels": {
            "system_instruction": "inline",
            "user_task_prompt": "inline",
            "task_context": "native-injection",
        },
    }


def test_write_projection_artifacts_uses_projected_content_for_primary(tmp_path: Path) -> None:
    run_inputs = ResolvedRunInputs(
        prompt="fallback prompt",
        model=ModelId("gpt-5.4"),
        appended_system_prompt="fallback system",
        user_turn_content="fallback user",
    )
    spec = OpenCodeLaunchSpec(prompt="fallback prompt", permission_resolver=_resolver())
    projected = ProjectedContent(
        system_prompt="projected system",
        user_turn_content="projected user",
        reference_routing=(),
        channels=ProjectionChannels(
            system_instruction="none",
            user_task_prompt="inline",
            task_context="inline",
        ),
    )
    launch_context = _make_launch_context(
        tmp_path=tmp_path,
        spec=spec,
        run_inputs=run_inputs,
        projected=projected,
    )
    log_dir = tmp_path / "primary"
    log_dir.mkdir(parents=True)

    write_projection_artifacts(log_dir=log_dir, launch_context=launch_context, surface="primary")

    assert (log_dir / "system-prompt.md").read_text(encoding="utf-8") == "projected system"
    assert (log_dir / "starting-prompt.md").read_text(encoding="utf-8") == "projected user"
    assert json.loads((log_dir / "projection-manifest.json").read_text(encoding="utf-8")) == {
        "harness": "opencode",
        "surface": "primary",
        "channels": {
            "system_instruction": "none",
            "user_task_prompt": "inline",
            "task_context": "inline",
        },
    }


def test_write_params_json_does_not_write_legacy_prompt_md(tmp_path: Path) -> None:
    project_paths = ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path)
    spawn_id = SpawnId("p123")
    request = SpawnRequest(prompt="prompt", model="gpt-5.4", harness="codex")

    _write_params_json(project_paths, spawn_id, request)

    log_dir = tmp_path / ".meridian" / "spawns" / str(spawn_id)
    assert (log_dir / "params.json").exists()
    assert not (log_dir / "prompt.md").exists()


@pytest.mark.asyncio
async def test_prepare_execution_handoff_transfers_session_scope_cleanup_to_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    session_state = {"entered": False, "exited": False}

    class _HarnessRegistry:
        def get_subprocess_harness(self, _harness_id: object) -> OpenCodeAdapter:
            return OpenCodeAdapter()

    @contextmanager
    def fake_session_execution_context(**_kwargs: object) -> Iterator[_SessionExecutionContext]:
        session_state["entered"] = True
        try:
            yield _SessionExecutionContext(
                chat_id="c1",
                work_id="w1",
                resolved_agent_name="agent-from-session",
                harness_session_id_observer=lambda _session_id: None,
            )
        finally:
            session_state["exited"] = True

    def fake_build_launch_context(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            resolved_request=kwargs["request"],
            harness=SimpleNamespace(id=HarnessId.OPENCODE),
            child_cwd=tmp_path,
        )

    monkeypatch.setattr(
        execute_module,
        "_session_execution_context",
        fake_session_execution_context,
    )
    monkeypatch.setattr(execute_module, "build_launch_context", fake_build_launch_context)

    def noop_write_projection_artifacts(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        execute_module,
        "write_projection_artifacts",
        noop_write_projection_artifacts,
    )

    handoff = await _prepare_execution_handoff(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p1"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast(
            "Any",
            SimpleNamespace(harness_registry=_HarnessRegistry(), artifacts=None),
        ),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        spawn_record=None,
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        autocompact=None,
        ctx=None,
    )

    assert session_state == {"entered": True, "exited": False}
    assert handoff.work_id == "w1"
    assert handoff.resolved_request.agent == "agent-from-session"

    handoff.session_exit_stack.close()

    assert session_state == {"entered": True, "exited": True}


@pytest.mark.asyncio
async def test_prepare_execution_handoff_closes_session_scope_when_later_preparation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    session_state = {"entered": False, "exited": False}

    class _HarnessRegistry:
        def get_subprocess_harness(self, _harness_id: object) -> OpenCodeAdapter:
            return OpenCodeAdapter()

    @contextmanager
    def fake_session_execution_context(**_kwargs: object) -> Iterator[_SessionExecutionContext]:
        session_state["entered"] = True
        try:
            yield _SessionExecutionContext(
                chat_id="c1",
                work_id=None,
                resolved_agent_name=None,
                harness_session_id_observer=lambda _session_id: None,
            )
        finally:
            session_state["exited"] = True

    def fake_build_launch_context(**_kwargs: object) -> SimpleNamespace:
        raise RuntimeError("boom after session scope entry")

    monkeypatch.setattr(
        execute_module,
        "_session_execution_context",
        fake_session_execution_context,
    )
    monkeypatch.setattr(execute_module, "build_launch_context", fake_build_launch_context)

    with pytest.raises(RuntimeError, match="boom after session scope entry"):
        await _prepare_execution_handoff(
            spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p1"))),
            request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
            runtime_request=LaunchRuntime(
                runtime_root=(tmp_path / ".runtime").as_posix(),
                project_paths_project_root=tmp_path.as_posix(),
                project_paths_execution_cwd=tmp_path.as_posix(),
            ),
            runtime=cast(
                "Any",
                SimpleNamespace(harness_registry=_HarnessRegistry(), artifacts=None),
            ),
            runtime_root=tmp_path / ".runtime",
            project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
            spawn_record=None,
            execution_cwd=tmp_path.as_posix(),
            work_id=None,
            autocompact=None,
            ctx=None,
        )

    assert session_state == {"entered": True, "exited": True}


@pytest.mark.parametrize("harness", ["claude", "opencode"])
@pytest.mark.asyncio
async def test_prepare_execution_handoff_keeps_native_child_fork_for_non_codex_harnesses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    class _HarnessRegistry:
        def get_subprocess_harness(self, _harness_id: object) -> SimpleNamespace:
            return SimpleNamespace(
                capabilities=SimpleNamespace(
                    supports_session_resume=True,
                    supports_session_fork=True,
                )
            )

    @contextmanager
    def fake_session_execution_context(**_kwargs: object) -> Iterator[_SessionExecutionContext]:
        yield _SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        )

    def fake_build_launch_context(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            resolved_request=kwargs["request"],
            harness=SimpleNamespace(id=HarnessId.OPENCODE),
            child_cwd=tmp_path,
        )

    def noop_write_projection_artifacts(**_kwargs: object) -> None:
        return None

    materialize_calls = {"count": 0}

    def fake_materialize_fork(**_kwargs: object) -> str:
        materialize_calls["count"] += 1
        return "unexpected-fork-id"

    monkeypatch.setattr(
        execute_module,
        "_session_execution_context",
        fake_session_execution_context,
    )
    monkeypatch.setattr(execute_module, "build_launch_context", fake_build_launch_context)
    monkeypatch.setattr(
        execute_module,
        "write_projection_artifacts",
        noop_write_projection_artifacts,
    )
    monkeypatch.setattr(execute_module, "materialize_fork", fake_materialize_fork)

    handoff = await _prepare_execution_handoff(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p-native"))),
        request=SpawnRequest(
            prompt="run it",
            model="gpt-5.4",
            harness=harness,
            session=SessionRequest(
                requested_harness_session_id="source-session",
                continue_harness=harness,
                continue_fork=True,
            ),
        ),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast(
            "Any",
            SimpleNamespace(harness_registry=_HarnessRegistry(), artifacts=None),
        ),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        spawn_record=None,
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        autocompact=None,
        ctx=None,
    )
    try:
        assert materialize_calls["count"] == 0
        assert handoff.resolved_request.session.requested_harness_session_id == "source-session"
        assert handoff.resolved_request.session.continue_fork is True
    finally:
        handoff.session_exit_stack.close()


@pytest.mark.asyncio
async def test_prepare_execution_handoff_materializes_child_fork_for_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    class _HarnessRegistry:
        def get_subprocess_harness(self, _harness_id: object) -> SimpleNamespace:
            return SimpleNamespace(
                capabilities=SimpleNamespace(
                    supports_session_resume=True,
                    supports_session_fork=True,
                )
            )

    @contextmanager
    def fake_session_execution_context(**_kwargs: object) -> Iterator[_SessionExecutionContext]:
        yield _SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        )

    def fake_build_launch_context(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            resolved_request=kwargs["request"],
            harness=SimpleNamespace(id=HarnessId.OPENCODE),
            child_cwd=tmp_path,
        )

    def noop_write_projection_artifacts(**_kwargs: object) -> None:
        return None

    materialize_args: dict[str, object] = {}

    def fake_materialize_fork(**kwargs: object) -> str:
        materialize_args.update(kwargs)
        return "forked-session"

    monkeypatch.setattr(
        execute_module,
        "_session_execution_context",
        fake_session_execution_context,
    )
    monkeypatch.setattr(execute_module, "build_launch_context", fake_build_launch_context)
    monkeypatch.setattr(
        execute_module,
        "write_projection_artifacts",
        noop_write_projection_artifacts,
    )
    monkeypatch.setattr(execute_module, "materialize_fork", fake_materialize_fork)

    handoff = await _prepare_execution_handoff(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p-codex"))),
        request=SpawnRequest(
            prompt="run it",
            model="gpt-5.4",
            harness="codex",
            session=SessionRequest(
                requested_harness_session_id="source-session",
                continue_harness="codex",
                continue_fork=True,
            ),
        ),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast(
            "Any",
            SimpleNamespace(harness_registry=_HarnessRegistry(), artifacts=None),
        ),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        spawn_record=None,
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        autocompact=None,
        ctx=None,
    )
    try:
        assert materialize_args["source_session_id"] == "source-session"
        assert materialize_args["spawn_id"] == SpawnId("p-codex")
        assert handoff.resolved_request.session.requested_harness_session_id == "forked-session"
        assert handoff.resolved_request.session.continue_fork is False
    finally:
        handoff.session_exit_stack.close()


def _start_background_spawn_row(
    *,
    tmp_path: Path,
    runtime_root: Path,
    spawn_id: SpawnId,
    prompt: str = "stored prompt",
    model: str = "stored-model",
    harness: str = "stored-harness",
) -> None:
    service = create_lifecycle_service(tmp_path, runtime_root)
    service.start(
        chat_id="c1",
        model=model,
        agent="",
        skills=(),
        skill_paths=(),
        harness=harness,
        kind="child",
        prompt=prompt,
        spawn_id=str(spawn_id),
        status="queued",
        launch_mode="background",
    )


def _background_launch_request(
    *,
    tmp_path: Path,
    prompt: str,
    harness: str,
    model: str = "gpt-5.4",
) -> BackgroundWorkerLaunchRequest:
    return BackgroundWorkerLaunchRequest(
        request=SpawnRequest(prompt=prompt, model=model, harness=harness),
        runtime=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
    )


@pytest.mark.parametrize(
    ("prompt", "harness", "expected_error"),
    [
        ("", "codex", "Missing prompt"),
        ("run it", "", "Missing harness"),
    ],
)
def test_execute_existing_spawn_terminalizes_missing_required_launch_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
    harness: str,
    expected_error: str,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    runtime_root = tmp_path / ".runtime"
    spawn_id = SpawnId("p1")
    _start_background_spawn_row(
        tmp_path=tmp_path,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    def fake_resolve_runtime_root(_project_root: Path) -> Path:
        return runtime_root

    def fake_build_runtime(_project_root: str, *, sink: object | None = None) -> SimpleNamespace:
        return SimpleNamespace(harness_registry=None, artifacts=None)

    monkeypatch.setattr(execute_module, "resolve_runtime_root", fake_resolve_runtime_root)
    monkeypatch.setattr(
        execute_module,
        "build_runtime",
        fake_build_runtime,
    )

    result = asyncio.run(
        _execute_existing_spawn(
            spawn_id=spawn_id,
            project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
            launch_request=_background_launch_request(
                tmp_path=tmp_path,
                prompt=prompt,
                harness=harness,
            ),
        )
    )

    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert result == 1
    assert record is not None
    assert record.status == "failed"
    assert record.terminal_origin == "launch_failure"
    assert record.error == expected_error


def test_finalize_launch_failure_sync_owns_fixed_terminal_tuple(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    spawn_id = SpawnId("p1")
    service = create_lifecycle_service(tmp_path, runtime_root)
    service.start(
        chat_id="c1",
        model="gpt-5.4",
        agent="",
        skills=(),
        skill_paths=(),
        harness="codex",
        kind="child",
        prompt="stored prompt",
        spawn_id=str(spawn_id),
        status="queued",
    )

    outcome = finalize_launch_failure_sync(runtime_root, tmp_path, spawn_id, "boom")

    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert outcome.wrote is True
    assert record is not None
    assert record.status == "failed"
    assert record.exit_code == 1
    assert record.terminal_origin == "launch_failure"
    assert record.error == "boom"


@pytest.mark.asyncio
async def test_finalize_launch_failure_async_matches_sync_tuple(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    spawn_id = SpawnId("p1")
    service = create_lifecycle_service(tmp_path, runtime_root)
    service.start(
        chat_id="c1",
        model="gpt-5.4",
        agent="",
        skills=(),
        skill_paths=(),
        harness="codex",
        kind="child",
        prompt="stored prompt",
        spawn_id=str(spawn_id),
        status="queued",
    )

    outcome = await finalize_launch_failure(runtime_root, tmp_path, spawn_id, "boom")

    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert outcome.wrote is True
    assert record is not None
    assert record.status == "failed"
    assert record.exit_code == 1
    assert record.terminal_origin == "launch_failure"
    assert record.error == "boom"


def test_execute_existing_spawn_allows_empty_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    runtime_root = tmp_path / ".runtime"
    spawn_id = SpawnId("p1")
    _start_background_spawn_row(
        tmp_path=tmp_path,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
        model="stored-model-must-not-be-used",
        harness="codex",
    )

    class _HarnessRegistry:
        def get_subprocess_harness(self, _harness_id: object) -> OpenCodeAdapter:
            return OpenCodeAdapter()

    @contextmanager
    def fake_session_execution_context(**_kwargs: object) -> Iterator[_SessionExecutionContext]:
        yield _SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        )

    captured: dict[str, object] = {}

    async def fake_execute_with_streaming(*args: object, **kwargs: object) -> int:
        captured["spawn"] = args[0]
        captured["request"] = kwargs["request"]
        return 0

    def fake_resolve_runtime_root(_project_root: Path) -> Path:
        return runtime_root

    def fake_build_runtime(_project_root: str, *, sink: object | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            harness_registry=_HarnessRegistry(),
            artifacts=None,
        )

    def fake_build_launch_context(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            resolved_request=kwargs["request"],
            harness=SimpleNamespace(id=HarnessId.OPENCODE),
            child_cwd=tmp_path,
        )

    def fake_write_projection_artifacts(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(execute_module, "resolve_runtime_root", fake_resolve_runtime_root)
    monkeypatch.setattr(
        execute_module,
        "build_runtime",
        fake_build_runtime,
    )
    monkeypatch.setattr(
        execute_module,
        "_session_execution_context",
        fake_session_execution_context,
    )
    monkeypatch.setattr(
        execute_module,
        "build_launch_context",
        fake_build_launch_context,
    )
    monkeypatch.setattr(
        execute_module,
        "write_projection_artifacts",
        fake_write_projection_artifacts,
    )
    monkeypatch.setattr(execute_module, "execute_with_streaming", fake_execute_with_streaming)

    result = asyncio.run(
        _execute_existing_spawn(
            spawn_id=spawn_id,
            project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
            launch_request=_background_launch_request(
                tmp_path=tmp_path,
                prompt="run it",
                harness="codex",
                model="",
            ),
        )
    )

    assert result == 0
    assert cast("Any", captured["spawn"]).model == ""
    assert cast("Any", captured["request"]).model == ""
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert record is not None
    assert record.status == "queued"


@pytest.mark.asyncio
async def test_launch_prepared_spawn_terminalizes_prerun_exception_as_launch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    runtime_root = tmp_path / ".runtime"
    spawn_id = SpawnId("p1")
    service = create_lifecycle_service(tmp_path, runtime_root)
    service.start(
        chat_id="c1",
        model="gpt-5.4",
        agent="",
        skills=(),
        skill_paths=(),
        harness="codex",
        kind="child",
        prompt="stored prompt",
        spawn_id=str(spawn_id),
        status="queued",
    )

    class _HarnessRegistry:
        def get_subprocess_harness(self, _harness_id: object) -> OpenCodeAdapter:
            return OpenCodeAdapter()

    @contextmanager
    def fake_session_execution_context(**_kwargs: object) -> Iterator[_SessionExecutionContext]:
        yield _SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        )

    def fake_build_launch_context(**_kwargs: object) -> SimpleNamespace:
        raise RuntimeError("boom before runner entry")

    async def fail_if_called(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("execute_with_streaming should not run after pre-run failure")

    monkeypatch.setattr(
        execute_module,
        "_session_execution_context",
        fake_session_execution_context,
    )
    monkeypatch.setattr(execute_module, "build_launch_context", fake_build_launch_context)
    monkeypatch.setattr(execute_module, "execute_with_streaming", fail_if_called)

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=spawn_id)),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
        runtime_request=LaunchRuntime(
            runtime_root=runtime_root.as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast(
            "Any",
            SimpleNamespace(harness_registry=_HarnessRegistry(), artifacts=None),
        ),
        runtime_root=runtime_root,
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert result == 1
    assert record is not None
    assert record.status == "failed"
    assert record.exit_code == 1
    assert record.terminal_origin == "launch_failure"
    assert record.error == "boom before runner entry"


@pytest.mark.asyncio
async def test_launch_prepared_spawn_uses_direct_build_launch_context_from_durable_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    runtime_root = tmp_path / ".runtime"
    spawn_id = SpawnId("p1")
    service = create_lifecycle_service(tmp_path, runtime_root)
    service.start(
        chat_id="c1",
        model="gpt-5.4",
        agent="",
        skills=(),
        skill_paths=(),
        harness="codex",
        kind="child",
        prompt="stored prompt",
        spawn_id=str(spawn_id),
        status="queued",
    )

    class _HarnessRegistry:
        def get_subprocess_harness(self, _harness_id: object) -> OpenCodeAdapter:
            return OpenCodeAdapter()

    @contextmanager
    def fake_session_execution_context(**_kwargs: object) -> Iterator[_SessionExecutionContext]:
        yield _SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        )

    captured: dict[str, object] = {}

    def fake_build_launch_context(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            argv=("opencode", "run"),
            spec=SimpleNamespace(),
            env=MappingProxyType({}),
            harness=SimpleNamespace(id=HarnessId.OPENCODE),
            child_cwd=tmp_path,
            report_output_path=tmp_path / "report.md",
            resolved_request=kwargs["request"],
        )

    async def fake_execute_with_streaming(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(
        execute_module,
        "_session_execution_context",
        fake_session_execution_context,
    )
    monkeypatch.setattr(execute_module, "build_launch_context", fake_build_launch_context)

    def noop_write_projection_artifacts(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        execute_module,
        "write_projection_artifacts",
        noop_write_projection_artifacts,
    )
    monkeypatch.setattr(execute_module, "execute_with_streaming", fake_execute_with_streaming)

    request = SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex")
    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=spawn_id)),
        request=request,
        runtime_request=LaunchRuntime(
            runtime_root=runtime_root.as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast(
            "Any",
            SimpleNamespace(harness_registry=_HarnessRegistry(), artifacts=None),
        ),
        runtime_root=runtime_root,
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 0
    assert isinstance(captured["request"], SpawnRequest)
    assert cast("SpawnRequest", captured["request"]).prompt == "run it"
    assert cast("LaunchRuntime", captured["runtime"]).composition_surface == (
        LaunchCompositionSurface.DIRECT
    )
    assert cast("LaunchRuntime", captured["runtime"]).argv_intent == LaunchArgvIntent.SPEC_ONLY


@pytest.mark.asyncio
async def test_launch_prepared_spawn_claude_overlay_updates_env_metadata_and_seeding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    overlay_root = tmp_path / "claude-config" / "p1"
    overlay_root.mkdir(parents=True)
    child_cwd = tmp_path / "child"
    child_cwd.mkdir()

    run_inputs = ResolvedRunInputs(
        prompt="run it",
        model=ModelId("gpt-5.4"),
        project_root=tmp_path.as_posix(),
    )
    base_launch_context = _make_launch_context(
        tmp_path=tmp_path,
        spec=OpenCodeLaunchSpec(prompt="run it", permission_resolver=_resolver()),
        run_inputs=run_inputs,
    )
    claude_launch_context = dataclass_replace(
        base_launch_context,
        harness=cast("Any", SimpleNamespace(id=HarnessId.CLAUDE)),
        child_cwd=child_cwd,
        env=MappingProxyType({"BASE": "1"}),
    )
    handoff = PreparedExecutionHandoff(
        resolved_request=SpawnRequest(
            prompt="run it",
            model="gpt-5.4",
            harness="claude",
            session=SessionRequest(
                requested_harness_session_id="session-1",
                source_execution_cwd=(tmp_path / "source").as_posix(),
                source_claude_config_dir=(tmp_path / "source-overlay").as_posix(),
            ),
        ),
        launch_context=claude_launch_context,
        session_context=_SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        ),
        session_exit_stack=ExitStack(),
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        harness_session_id_observer=lambda _session_id: None,
    )

    updates: list[tuple[str, str]] = []
    session_updates: list[tuple[str, str]] = []
    seeded: dict[str, object] = {}

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return handoff

    def fake_prepare_isolated_claude_config(
        *,
        runtime_root: Path,
        spawn_id: str,
    ) -> tuple[Path, str]:
        assert runtime_root == tmp_path / ".runtime"
        assert spawn_id == "p1"
        return overlay_root, ""

    def fake_update_spawn(
        _runtime_root: Path,
        _spawn_id: SpawnId,
        *,
        claude_config_dir: str | None = None,
        **_kwargs: object,
    ) -> object:
        if claude_config_dir is not None:
            updates.append(("spawn", claude_config_dir))
        return SimpleNamespace(wrote=True, entered_finalizing=False)

    def fake_update_session_claude_config_dir(
        _runtime_root: Path,
        chat_id: str,
        *,
        claude_config_dir: str,
    ) -> None:
        session_updates.append((chat_id, claude_config_dir))

    def fake_seed_session_access(**kwargs: object) -> None:
        seeded.update(kwargs)

    async def fake_invoke_runner(
        prepared_handoff: PreparedExecutionHandoff,
        **_kwargs: object,
    ) -> int:
        assert prepared_handoff.launch_context.env["CLAUDE_CONFIG_DIR"] == overlay_root.as_posix()
        assert (
            prepared_handoff.launch_context.env[MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV]
            == ""
        )
        assert updates == [("spawn", overlay_root.as_posix())]
        assert session_updates == [("c1", overlay_root.as_posix())]
        return 0

    def noop_cleanup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(
        execute_module,
        "prepare_isolated_claude_config",
        fake_prepare_isolated_claude_config,
    )
    monkeypatch.setattr(execute_module.spawn_store, "update_spawn", fake_update_spawn)
    monkeypatch.setattr(
        execute_module,
        "update_session_claude_config_dir",
        fake_update_session_claude_config_dir,
    )
    monkeypatch.setattr(
        execute_module,
        "ensure_claude_session_accessible",
        fake_seed_session_access,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)
    monkeypatch.setattr(execute_module, "_cleanup_child_claude_overlay", noop_cleanup)

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p1"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast("Any", SimpleNamespace(harness_registry=SimpleNamespace(), artifacts=None)),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 0
    assert seeded["source_session_id"] == "session-1"
    assert seeded["source_cwd"] == Path((tmp_path / "source").as_posix())
    assert seeded["child_cwd"] == child_cwd
    assert seeded["source_config_root"] == Path((tmp_path / "source-overlay").as_posix())
    assert seeded["target_config_root"] == overlay_root


@pytest.mark.asyncio
async def test_launch_prepared_spawn_claude_overlay_fallback_root_seeds_explicit_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    fallback_root = tmp_path / "shared-claude-root"
    fallback_root.mkdir(parents=True)
    child_cwd = tmp_path / "child"
    child_cwd.mkdir()

    run_inputs = ResolvedRunInputs(
        prompt="run it",
        model=ModelId("gpt-5.4"),
        project_root=tmp_path.as_posix(),
    )
    base_launch_context = _make_launch_context(
        tmp_path=tmp_path,
        spec=OpenCodeLaunchSpec(prompt="run it", permission_resolver=_resolver()),
        run_inputs=run_inputs,
    )
    claude_launch_context = dataclass_replace(
        base_launch_context,
        harness=cast("Any", SimpleNamespace(id=HarnessId.CLAUDE)),
        child_cwd=child_cwd,
        env=MappingProxyType({"BASE": "1"}),
    )
    handoff = PreparedExecutionHandoff(
        resolved_request=SpawnRequest(
            prompt="run it",
            model="gpt-5.4",
            harness="claude",
            session=SessionRequest(
                requested_harness_session_id="session-1",
                source_execution_cwd=(tmp_path / "source").as_posix(),
                source_claude_config_dir=(tmp_path / "source-overlay").as_posix(),
            ),
        ),
        launch_context=claude_launch_context,
        session_context=_SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        ),
        session_exit_stack=ExitStack(),
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        harness_session_id_observer=lambda _session_id: None,
    )

    seeded: dict[str, object] = {}

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return handoff

    def fake_prepare_isolated_claude_config(
        *,
        runtime_root: Path,
        spawn_id: str,
    ) -> tuple[None, str]:
        assert runtime_root == tmp_path / ".runtime"
        assert spawn_id == "p-fallback"
        return None, fallback_root.as_posix()

    def fake_update_spawn(
        *_args: object,
        **_kwargs: object,
    ) -> object:
        return SimpleNamespace(wrote=True, entered_finalizing=False)

    def fake_update_session(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_seed_session_access(**kwargs: object) -> None:
        seeded.update(kwargs)

    async def fake_invoke_runner(
        prepared_handoff: PreparedExecutionHandoff,
        **_kwargs: object,
    ) -> int:
        assert prepared_handoff.launch_context.env["CLAUDE_CONFIG_DIR"] == fallback_root.as_posix()
        assert prepared_handoff.launch_context.env["BASE"] == "1"
        return 0

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cleanup should not run without isolated overlay root")

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(
        execute_module,
        "prepare_isolated_claude_config",
        fake_prepare_isolated_claude_config,
    )
    monkeypatch.setattr(execute_module.spawn_store, "update_spawn", fake_update_spawn)
    monkeypatch.setattr(
        execute_module,
        "update_session_claude_config_dir",
        fake_update_session,
    )
    monkeypatch.setattr(
        execute_module,
        "ensure_claude_session_accessible",
        fake_seed_session_access,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)
    monkeypatch.setattr(execute_module, "_cleanup_child_claude_overlay", fail_cleanup)

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p-fallback"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast("Any", SimpleNamespace(harness_registry=SimpleNamespace(), artifacts=None)),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 0
    assert seeded["target_config_root"] == fallback_root


@pytest.mark.asyncio
async def test_launch_prepared_spawn_claude_overlay_default_root_fallback_ignores_parent_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    parent_overlay = tmp_path / "parent-overlay"
    parent_overlay.mkdir(parents=True)
    durable_default_root = tmp_path / "durable-default-root"
    durable_default_root.mkdir(parents=True)
    child_cwd = tmp_path / "child"
    child_cwd.mkdir()

    run_inputs = ResolvedRunInputs(
        prompt="run it",
        model=ModelId("gpt-5.4"),
        project_root=tmp_path.as_posix(),
    )
    base_launch_context = _make_launch_context(
        tmp_path=tmp_path,
        spec=OpenCodeLaunchSpec(prompt="run it", permission_resolver=_resolver()),
        run_inputs=run_inputs,
    )
    claude_launch_context = dataclass_replace(
        base_launch_context,
        harness=cast("Any", SimpleNamespace(id=HarnessId.CLAUDE)),
        child_cwd=child_cwd,
        env=MappingProxyType({"CLAUDE_CONFIG_DIR": parent_overlay.as_posix(), "BASE": "1"}),
    )
    handoff = PreparedExecutionHandoff(
        resolved_request=SpawnRequest(
            prompt="run it",
            model="gpt-5.4",
            harness="claude",
            session=SessionRequest(
                requested_harness_session_id="session-1",
                source_execution_cwd=(tmp_path / "source").as_posix(),
                source_claude_config_dir=(tmp_path / "source-overlay").as_posix(),
            ),
        ),
        launch_context=claude_launch_context,
        session_context=_SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        ),
        session_exit_stack=ExitStack(),
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        harness_session_id_observer=lambda _session_id: None,
    )

    seeded: dict[str, object] = {}
    spawn_updates: list[str] = []
    session_updates: list[tuple[str, str]] = []
    resolver_calls: list[tuple[Path | None, str]] = []

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return handoff

    def fake_prepare_isolated_claude_config(
        *,
        runtime_root: Path,
        spawn_id: str,
    ) -> tuple[None, str]:
        assert runtime_root == tmp_path / ".runtime"
        assert spawn_id == "p-default-root"
        return None, ""

    def fake_resolve_claude_overlay_roots(
        *,
        isolated_config_root: Path | None,
        original_config_env: str,
    ) -> SimpleNamespace:
        resolver_calls.append((isolated_config_root, original_config_env))
        return SimpleNamespace(
            effective_config_root=durable_default_root,
            materialization_root=durable_default_root,
        )

    def fake_update_spawn(
        _runtime_root: Path,
        _spawn_id: SpawnId,
        *,
        claude_config_dir: str | None = None,
        **_kwargs: object,
    ) -> object:
        if claude_config_dir is not None:
            spawn_updates.append(claude_config_dir)
        return SimpleNamespace(wrote=True, entered_finalizing=False)

    def fake_update_session(
        _runtime_root: Path,
        chat_id: str,
        *,
        claude_config_dir: str,
    ) -> None:
        session_updates.append((chat_id, claude_config_dir))

    def fake_seed_session_access(**kwargs: object) -> None:
        seeded.update(kwargs)

    async def fake_invoke_runner(
        prepared_handoff: PreparedExecutionHandoff,
        **_kwargs: object,
    ) -> int:
        assert (
            prepared_handoff.launch_context.env["CLAUDE_CONFIG_DIR"]
            == durable_default_root.as_posix()
        )
        assert (
            prepared_handoff.launch_context.env[MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV]
            == ""
        )
        assert prepared_handoff.launch_context.env["BASE"] == "1"
        return 0

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cleanup should not run without isolated overlay root")

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(
        execute_module,
        "prepare_isolated_claude_config",
        fake_prepare_isolated_claude_config,
    )
    monkeypatch.setattr(
        execute_module,
        "resolve_claude_overlay_roots",
        fake_resolve_claude_overlay_roots,
    )
    monkeypatch.setattr(execute_module.spawn_store, "update_spawn", fake_update_spawn)
    monkeypatch.setattr(
        execute_module,
        "update_session_claude_config_dir",
        fake_update_session,
    )
    monkeypatch.setattr(
        execute_module,
        "ensure_claude_session_accessible",
        fake_seed_session_access,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)
    monkeypatch.setattr(execute_module, "_cleanup_child_claude_overlay", fail_cleanup)

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p-default-root"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast("Any", SimpleNamespace(harness_registry=SimpleNamespace(), artifacts=None)),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 0
    assert resolver_calls == [(None, "")]
    assert seeded["target_config_root"] == durable_default_root
    assert seeded["target_config_root"] != parent_overlay
    assert spawn_updates == [durable_default_root.as_posix()]
    assert session_updates == [("c1", durable_default_root.as_posix())]


@pytest.mark.asyncio
async def test_launch_prepared_spawn_claude_overlay_reseeds_same_cwd_across_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    overlay_root = tmp_path / "claude-config" / "p-same"
    overlay_root.mkdir(parents=True)
    child_cwd = tmp_path / "project"
    child_cwd.mkdir()
    parent_overlay = tmp_path / "parent-overlay"

    run_inputs = ResolvedRunInputs(
        prompt="run it",
        model=ModelId("gpt-5.4"),
        project_root=tmp_path.as_posix(),
    )
    base_launch_context = _make_launch_context(
        tmp_path=tmp_path,
        spec=OpenCodeLaunchSpec(prompt="run it", permission_resolver=_resolver()),
        run_inputs=run_inputs,
    )
    claude_launch_context = dataclass_replace(
        base_launch_context,
        harness=cast("Any", SimpleNamespace(id=HarnessId.CLAUDE)),
        child_cwd=child_cwd,
        env=MappingProxyType({"CLAUDE_CONFIG_DIR": parent_overlay.as_posix(), "BASE": "1"}),
    )
    handoff = PreparedExecutionHandoff(
        resolved_request=SpawnRequest(
            prompt="run it",
            model="gpt-5.4",
            harness="claude",
            session=SessionRequest(
                requested_harness_session_id="session-1",
                source_execution_cwd=child_cwd.as_posix(),
                source_claude_config_dir=(tmp_path / "source-overlay").as_posix(),
            ),
        ),
        launch_context=claude_launch_context,
        session_context=_SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        ),
        session_exit_stack=ExitStack(),
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        harness_session_id_observer=lambda _session_id: None,
    )

    seeded: dict[str, object] = {}

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return handoff

    def fake_prepare_isolated_claude_config(
        *,
        runtime_root: Path,
        spawn_id: str,
    ) -> tuple[Path, str]:
        assert runtime_root == tmp_path / ".runtime"
        assert spawn_id == "p-same"
        return overlay_root, ""

    def fake_update_spawn(
        *_args: object,
        **_kwargs: object,
    ) -> object:
        return SimpleNamespace(wrote=True, entered_finalizing=False)

    def fake_update_session(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_seed_session_access(**kwargs: object) -> None:
        seeded.update(kwargs)

    async def fake_invoke_runner(
        prepared_handoff: PreparedExecutionHandoff,
        **_kwargs: object,
    ) -> int:
        assert prepared_handoff.launch_context.env["CLAUDE_CONFIG_DIR"] == overlay_root.as_posix()
        assert prepared_handoff.launch_context.env["BASE"] == "1"
        return 0

    def noop_cleanup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(
        execute_module,
        "prepare_isolated_claude_config",
        fake_prepare_isolated_claude_config,
    )
    monkeypatch.setattr(execute_module.spawn_store, "update_spawn", fake_update_spawn)
    monkeypatch.setattr(
        execute_module,
        "update_session_claude_config_dir",
        fake_update_session,
    )
    monkeypatch.setattr(
        execute_module,
        "ensure_claude_session_accessible",
        fake_seed_session_access,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)
    monkeypatch.setattr(execute_module, "_cleanup_child_claude_overlay", noop_cleanup)

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p-same"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast("Any", SimpleNamespace(harness_registry=SimpleNamespace(), artifacts=None)),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 0
    assert seeded["source_cwd"] == child_cwd
    assert seeded["child_cwd"] == child_cwd
    assert seeded["source_config_root"] == Path((tmp_path / "source-overlay").as_posix())
    assert seeded["target_config_root"] == overlay_root


@pytest.mark.asyncio
async def test_launch_prepared_spawn_claude_overlay_cleanup_materializes_before_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    overlay_root = tmp_path / "claude-config" / "p2"
    overlay_root.mkdir(parents=True)
    durable_root = tmp_path / "durable-claude"
    durable_root.mkdir(parents=True)
    child_cwd = tmp_path / "child"
    child_cwd.mkdir()

    run_inputs = ResolvedRunInputs(
        prompt="run it",
        model=ModelId("gpt-5.4"),
        project_root=tmp_path.as_posix(),
    )
    base_launch_context = _make_launch_context(
        tmp_path=tmp_path,
        spec=OpenCodeLaunchSpec(prompt="run it", permission_resolver=_resolver()),
        run_inputs=run_inputs,
    )
    claude_launch_context = dataclass_replace(
        base_launch_context,
        harness=cast("Any", SimpleNamespace(id=HarnessId.CLAUDE)),
        child_cwd=child_cwd,
    )
    handoff = PreparedExecutionHandoff(
        resolved_request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        launch_context=claude_launch_context,
        session_context=_SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        ),
        session_exit_stack=ExitStack(),
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        harness_session_id_observer=lambda _session_id: None,
    )

    cleanup_calls: list[tuple[Path | None, Path | None]] = []
    spawn_updates: list[str] = []
    session_updates: list[tuple[str, str]] = []

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return handoff

    def fake_prepare_isolated_claude_config(
        *,
        runtime_root: Path,
        spawn_id: str,
    ) -> tuple[Path, str]:
        assert runtime_root == tmp_path / ".runtime"
        assert spawn_id == "p2"
        return overlay_root, durable_root.as_posix()

    async def fake_invoke_runner(
        _prepared_handoff: PreparedExecutionHandoff,
        **_kwargs: object,
    ) -> int:
        return 0

    def fake_update_spawn(
        _runtime_root: Path,
        _spawn_id: SpawnId,
        *,
        claude_config_dir: str | None = None,
        **_kwargs: object,
    ) -> object:
        if claude_config_dir is not None:
            spawn_updates.append(claude_config_dir)
        return SimpleNamespace(wrote=True, entered_finalizing=False)

    def fake_update_session(
        _runtime_root: Path,
        chat_id: str,
        *,
        claude_config_dir: str,
    ) -> None:
        session_updates.append((chat_id, claude_config_dir))

    def fake_cleanup_claude_overlay(
        overlay_root: Path | None,
        *,
        canonical_root: Path | None = None,
        remove_overlay: object | None = None,
    ) -> SimpleNamespace:
        cleanup_calls.append((overlay_root, canonical_root))
        assert canonical_root == durable_root
        return SimpleNamespace(materialization_root=durable_root, removed=True, materialized=True)

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(
        execute_module,
        "prepare_isolated_claude_config",
        fake_prepare_isolated_claude_config,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)
    monkeypatch.setattr(execute_module.spawn_store, "update_spawn", fake_update_spawn)
    monkeypatch.setattr(
        execute_module,
        "update_session_claude_config_dir",
        fake_update_session,
    )
    monkeypatch.setattr(
        execute_module,
        "cleanup_claude_overlay",
        fake_cleanup_claude_overlay,
    )

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p2"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast("Any", SimpleNamespace(harness_registry=SimpleNamespace(), artifacts=None)),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 0
    assert cleanup_calls == [(overlay_root, durable_root)]
    assert spawn_updates == [overlay_root.as_posix(), durable_root.as_posix()]
    assert session_updates == [
        ("c1", overlay_root.as_posix()),
        ("c1", durable_root.as_posix()),
    ]


@pytest.mark.asyncio
async def test_launch_prepared_spawn_skips_durable_metadata_on_partial_materialization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    overlay_root = tmp_path / "claude-config" / "p2"
    overlay_root.mkdir(parents=True)
    durable_root = tmp_path / "durable-claude"
    durable_root.mkdir(parents=True)
    child_cwd = tmp_path / "child"
    child_cwd.mkdir()

    run_inputs = ResolvedRunInputs(
        prompt="run it",
        model=ModelId("gpt-5.4"),
        project_root=tmp_path.as_posix(),
    )
    base_launch_context = _make_launch_context(
        tmp_path=tmp_path,
        spec=OpenCodeLaunchSpec(prompt="run it", permission_resolver=_resolver()),
        run_inputs=run_inputs,
    )
    claude_launch_context = dataclass_replace(
        base_launch_context,
        harness=cast("Any", SimpleNamespace(id=HarnessId.CLAUDE)),
        child_cwd=child_cwd,
    )
    handoff = PreparedExecutionHandoff(
        resolved_request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        launch_context=claude_launch_context,
        session_context=_SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        ),
        session_exit_stack=ExitStack(),
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        harness_session_id_observer=lambda _session_id: None,
    )

    spawn_updates: list[str] = []
    session_updates: list[tuple[str, str]] = []

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return handoff

    def fake_prepare_isolated_claude_config(
        *,
        runtime_root: Path,
        spawn_id: str,
    ) -> tuple[Path, str]:
        assert runtime_root == tmp_path / ".runtime"
        assert spawn_id == "p2"
        return overlay_root, durable_root.as_posix()

    async def fake_invoke_runner(
        _prepared_handoff: PreparedExecutionHandoff,
        **_kwargs: object,
    ) -> int:
        return 0

    def fake_update_spawn(
        _runtime_root: Path,
        _spawn_id: SpawnId,
        *,
        claude_config_dir: str | None = None,
        **_kwargs: object,
    ) -> object:
        if claude_config_dir is not None:
            spawn_updates.append(claude_config_dir)
        return SimpleNamespace(wrote=True, entered_finalizing=False)

    def fake_update_session(
        _runtime_root: Path,
        chat_id: str,
        *,
        claude_config_dir: str,
    ) -> None:
        session_updates.append((chat_id, claude_config_dir))

    def fake_cleanup_claude_overlay(
        overlay_root: Path | None,
        *,
        canonical_root: Path | None = None,
        remove_overlay: object | None = None,
    ) -> SimpleNamespace:
        _ = (overlay_root, remove_overlay)
        assert canonical_root == durable_root
        return SimpleNamespace(
            materialization_root=durable_root,
            removed=True,
            materialized=False,
        )

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(
        execute_module,
        "prepare_isolated_claude_config",
        fake_prepare_isolated_claude_config,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)
    monkeypatch.setattr(execute_module.spawn_store, "update_spawn", fake_update_spawn)
    monkeypatch.setattr(
        execute_module,
        "update_session_claude_config_dir",
        fake_update_session,
    )
    monkeypatch.setattr(
        execute_module,
        "cleanup_claude_overlay",
        fake_cleanup_claude_overlay,
    )

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p2"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast("Any", SimpleNamespace(harness_registry=SimpleNamespace(), artifacts=None)),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 0
    assert spawn_updates == [overlay_root.as_posix()]
    assert session_updates == [("c1", overlay_root.as_posix())]


def test_cleanup_child_claude_overlay_materializes_to_original_root_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    overlay_root = tmp_path / "claude-config" / "child-spawn"
    overlay_root.mkdir(parents=True)
    parent_overlay_root = tmp_path / "claude-config" / "parent-spawn"
    parent_overlay_root.mkdir(parents=True)
    durable_root = tmp_path / "durable-claude"
    durable_root.mkdir(parents=True)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", parent_overlay_root.as_posix())
    monkeypatch.setenv(
        MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV,
        durable_root.as_posix(),
    )

    captured: dict[str, object] = {}

    def fake_cleanup_claude_overlay(
        overlay_root: Path | None,
        *,
        canonical_root: Path | None = None,
        remove_overlay: object | None = None,
    ) -> SimpleNamespace:
        captured["overlay_root"] = overlay_root
        captured["canonical_root"] = canonical_root
        captured["remove_overlay"] = remove_overlay
        return SimpleNamespace(materialization_root=durable_root, removed=True, materialized=True)

    monkeypatch.setattr(
        execute_module,
        "cleanup_claude_overlay",
        fake_cleanup_claude_overlay,
    )

    execute_module._cleanup_child_claude_overlay(
        isolated_config_root=overlay_root,
        spawn_id=SpawnId("p-clean"),
    )

    assert captured["overlay_root"] == overlay_root
    assert captured["canonical_root"] == durable_root
    assert captured["canonical_root"] != parent_overlay_root


@pytest.mark.asyncio
async def test_launch_prepared_spawn_claude_overlay_metadata_failure_cleans_overlay_and_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    class _TrackingExitStack:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    overlay_root = tmp_path / "claude-config" / "p-meta"
    overlay_root.mkdir(parents=True)
    parent_overlay_root = tmp_path / "claude-config" / "parent-overlay"
    parent_overlay_root.mkdir(parents=True)
    durable_default_root = tmp_path / "durable-default-root"
    durable_default_root.mkdir(parents=True)
    run_inputs = ResolvedRunInputs(
        prompt="run it",
        model=ModelId("gpt-5.4"),
        project_root=tmp_path.as_posix(),
    )
    base_launch_context = _make_launch_context(
        tmp_path=tmp_path,
        spec=OpenCodeLaunchSpec(prompt="run it", permission_resolver=_resolver()),
        run_inputs=run_inputs,
    )
    claude_launch_context = dataclass_replace(
        base_launch_context,
        harness=cast("Any", SimpleNamespace(id=HarnessId.CLAUDE)),
        child_cwd=tmp_path,
        env=MappingProxyType({"CLAUDE_CONFIG_DIR": parent_overlay_root.as_posix()}),
    )
    exit_stack = _TrackingExitStack()
    handoff = PreparedExecutionHandoff(
        resolved_request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        launch_context=claude_launch_context,
        session_context=_SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        ),
        session_exit_stack=cast("Any", exit_stack),
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        harness_session_id_observer=lambda _session_id: None,
    )

    finalized_errors: list[str] = []
    cleanup_calls: list[tuple[str, Path, Path | None]] = []
    resolver_calls: list[tuple[Path | None, str]] = []

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return handoff

    def fake_prepare_isolated_claude_config(
        *,
        runtime_root: Path,
        spawn_id: str,
    ) -> tuple[Path, str]:
        assert runtime_root == tmp_path / ".runtime"
        assert spawn_id == "p4"
        return overlay_root, ""

    def fake_update_spawn(
        *_args: object,
        **_kwargs: object,
    ) -> object:
        raise RuntimeError("spawn metadata failed")

    def fake_resolve_claude_overlay_roots(
        *,
        isolated_config_root: Path | None,
        original_config_env: str,
    ) -> SimpleNamespace:
        resolver_calls.append((isolated_config_root, original_config_env))
        return SimpleNamespace(
            effective_config_root=overlay_root,
            materialization_root=durable_default_root,
        )

    def fake_cleanup_claude_overlay(
        overlay_root_arg: Path | None,
        *,
        canonical_root: Path | None = None,
        remove_overlay: object | None = None,
    ) -> SimpleNamespace:
        cleanup_calls.append(("cleanup", cast("Path", overlay_root_arg), canonical_root))
        return SimpleNamespace(
            materialization_root=durable_default_root,
            removed=True,
            materialized=True,
        )

    async def fake_finalize_launch_failure(
        _runtime_root: Path,
        _project_root: Path,
        _spawn_id: SpawnId,
        error: str,
    ) -> None:
        finalized_errors.append(error)

    async def fail_if_called(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("runner should not execute after metadata update failure")

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(
        execute_module,
        "prepare_isolated_claude_config",
        fake_prepare_isolated_claude_config,
    )
    monkeypatch.setattr(
        execute_module,
        "resolve_claude_overlay_roots",
        fake_resolve_claude_overlay_roots,
    )
    monkeypatch.setattr(execute_module.spawn_store, "update_spawn", fake_update_spawn)
    monkeypatch.setattr(
        execute_module,
        "cleanup_claude_overlay",
        fake_cleanup_claude_overlay,
    )
    monkeypatch.setattr(
        execute_module,
        "finalize_launch_failure",
        fake_finalize_launch_failure,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fail_if_called)

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p4"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast("Any", SimpleNamespace(harness_registry=SimpleNamespace(), artifacts=None)),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 1
    assert finalized_errors == ["spawn metadata failed"]
    assert resolver_calls == [(overlay_root, "")]
    assert cleanup_calls == [("cleanup", overlay_root, durable_default_root)]
    assert exit_stack.closed is True


@pytest.mark.asyncio
async def test_launch_prepared_spawn_does_not_finalize_launch_failure_on_teardown_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    class _ExplodingExitStack:
        def close(self) -> None:
            raise RuntimeError("teardown failed")

    captured = {"finalize_calls": 0, "runner_calls": 0}

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return PreparedExecutionHandoff(
            resolved_request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
            launch_context=cast(
                "Any",
                SimpleNamespace(
                    harness=SimpleNamespace(id=HarnessId.OPENCODE),
                    child_cwd=tmp_path,
                    env=MappingProxyType({}),
                ),
            ),
            session_context=_SessionExecutionContext(
                chat_id="c1",
                work_id=None,
                resolved_agent_name=None,
                harness_session_id_observer=lambda _session_id: None,
            ),
            session_exit_stack=cast("Any", _ExplodingExitStack()),
            execution_cwd=tmp_path.as_posix(),
            work_id=None,
            harness_session_id_observer=lambda _session_id: None,
        )

    async def fake_invoke_runner(
        _handoff: PreparedExecutionHandoff,
        **_kwargs: object,
    ) -> int:
        captured["runner_calls"] += 1
        return 0

    async def fake_finalize_launch_failure(*_args: object, **_kwargs: object) -> None:
        captured["finalize_calls"] += 1

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fake_invoke_runner)
    monkeypatch.setattr(
        execute_module,
        "finalize_launch_failure",
        fake_finalize_launch_failure,
    )

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p1"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast("Any", SimpleNamespace(harness_registry=SimpleNamespace(), artifacts=None)),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 0
    assert captured == {"finalize_calls": 0, "runner_calls": 1}


@pytest.mark.asyncio
async def test_launch_prepared_spawn_claude_prerun_failure_cleans_overlay_and_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    class _TrackingExitStack:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    overlay_root = tmp_path / "claude-config" / "p3"
    overlay_root.mkdir(parents=True)
    exit_stack = _TrackingExitStack()
    handoff = PreparedExecutionHandoff(
        resolved_request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        launch_context=cast(
            "Any",
            SimpleNamespace(
                harness=SimpleNamespace(id=HarnessId.CLAUDE),
                child_cwd=tmp_path,
                env=MappingProxyType({}),
            ),
        ),
        session_context=_SessionExecutionContext(
            chat_id="c1",
            work_id=None,
            resolved_agent_name=None,
            harness_session_id_observer=lambda _session_id: None,
        ),
        session_exit_stack=cast("Any", exit_stack),
        execution_cwd=tmp_path.as_posix(),
        work_id=None,
        harness_session_id_observer=lambda _session_id: None,
    )

    finalized_errors: list[str] = []
    cleaned_overlays: list[tuple[Path, SpawnId]] = []

    async def fake_prepare_execution_handoff(**_kwargs: object) -> PreparedExecutionHandoff:
        return handoff

    def fake_prepare_child_claude_overlay(**_kwargs: object) -> object:
        return SimpleNamespace(
            isolated_config_root=overlay_root,
            materialization_root=overlay_root,
            effective_config_root=overlay_root,
        )

    def fake_seed_child_claude_session_access(**_kwargs: object) -> None:
        raise RuntimeError("seed failed")

    async def fake_finalize_launch_failure(
        _runtime_root: Path,
        _project_root: Path,
        _spawn_id: SpawnId,
        error: str,
    ) -> None:
        finalized_errors.append(error)

    async def fail_if_called(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("runner should not execute after Claude pre-run setup failure")

    def fake_cleanup_child_claude_overlay(
        *,
        isolated_config_root: Path | None,
        spawn_id: SpawnId,
        canonical_root: Path | None = None,
    ) -> None:
        assert isolated_config_root is not None
        cleaned_overlays.append((isolated_config_root, spawn_id))

    monkeypatch.setattr(
        execute_module,
        "_prepare_execution_handoff",
        fake_prepare_execution_handoff,
    )
    monkeypatch.setattr(
        execute_module,
        "_prepare_child_claude_overlay",
        fake_prepare_child_claude_overlay,
    )
    monkeypatch.setattr(
        execute_module,
        "_seed_child_claude_session_access",
        fake_seed_child_claude_session_access,
    )
    monkeypatch.setattr(
        execute_module,
        "finalize_launch_failure",
        fake_finalize_launch_failure,
    )
    monkeypatch.setattr(execute_module, "_invoke_runner", fail_if_called)
    monkeypatch.setattr(
        execute_module,
        "_cleanup_child_claude_overlay",
        fake_cleanup_child_claude_overlay,
    )

    result = await launch_prepared_spawn(
        spawn=cast("Any", SimpleNamespace(spawn_id=SpawnId("p3"))),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="claude"),
        runtime_request=LaunchRuntime(
            runtime_root=(tmp_path / ".runtime").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        runtime=cast("Any", SimpleNamespace(harness_registry=SimpleNamespace(), artifacts=None)),
        runtime_root=tmp_path / ".runtime",
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        execution_cwd=tmp_path.as_posix(),
    )

    assert result == 1
    assert finalized_errors == ["seed failed"]
    assert exit_stack.closed is True
    assert cleaned_overlays == [(overlay_root, SpawnId("p3"))]


def test_execute_spawn_blocking_routes_through_launch_prepared_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    spawn_id = SpawnId("p123")
    captured: dict[str, object] = {}
    real_asyncio_run = asyncio.run

    def fake_init_spawn(**_kwargs: object) -> Any:
        return SimpleNamespace(
            spawn=SimpleNamespace(spawn_id=spawn_id),
            runtime_root=tmp_path / ".runtime",
            current_depth=0,
            work_id=None,
        )

    async def fake_launch_prepared_spawn(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    def fake_resolve_project_config_paths(*, project_root: str | Path) -> ProjectConfigPaths:
        project_path = Path(project_root)
        return ProjectConfigPaths(
            project_root=project_path,
            execution_cwd=project_path,
        )

    def fake_resolve_child_execution_cwd(**_kwargs: object) -> Path:
        return tmp_path

    def noop_write_params_json(*_args: object, **_kwargs: object) -> None:
        return None

    def run_coro(coro: Any) -> Any:
        return real_asyncio_run(coro)

    def fake_read_spawn_row(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            status="succeeded",
            duration_secs=1.25,
            input_tokens=3,
            output_tokens=5,
        )

    monkeypatch.setattr(execute_module, "_init_spawn", fake_init_spawn)
    monkeypatch.setattr(
        execute_module,
        "resolve_project_config_paths",
        fake_resolve_project_config_paths,
    )
    monkeypatch.setattr(
        execute_module,
        "resolve_child_execution_cwd",
        fake_resolve_child_execution_cwd,
    )
    monkeypatch.setattr(execute_module, "_write_params_json", noop_write_params_json)
    monkeypatch.setattr(execute_module, "launch_prepared_spawn", fake_launch_prepared_spawn)
    monkeypatch.setattr(execute_module.asyncio, "run", run_coro)
    monkeypatch.setattr(
        execute_module,
        "read_spawn_row",
        fake_read_spawn_row,
    )

    result = execute_spawn_blocking(
        payload=cast("Any", SimpleNamespace(desc="", work="", debug=False, stream=False)),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
        runtime=cast("Any", SimpleNamespace(project_root=tmp_path, sink=None)),
    )

    assert result.status == "succeeded"
    assert cast("Any", captured["spawn"]).spawn_id == spawn_id
    assert cast("Any", captured["request"]).prompt == "run it"
    assert cast("Any", captured["project_paths"]).project_root == tmp_path
    assert captured["execution_cwd"] == tmp_path.as_posix()


def test_execute_spawn_blocking_backstop_finalizes_uncaught_helper_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    spawn_id = SpawnId("p124")
    runtime_root = tmp_path / ".runtime"
    captured: dict[str, object] = {}
    real_asyncio_run = asyncio.run

    def fake_init_spawn(**_kwargs: object) -> Any:
        return SimpleNamespace(
            spawn=SimpleNamespace(spawn_id=spawn_id),
            runtime_root=runtime_root,
            current_depth=0,
            work_id=None,
        )

    async def exploding_launch_prepared_spawn(**_kwargs: object) -> int:
        raise RuntimeError("helper escaped")

    def fake_finalize_launch_failure_sync(
        runtime_root: Path,
        project_root: Path,
        spawn_id: SpawnId,
        error: str,
    ) -> bool:
        captured.update(
            {
                "runtime_root": runtime_root,
                "project_root": project_root,
                "spawn_id": spawn_id,
                "error": error,
            }
        )
        return True

    def fake_resolve_project_config_paths(*, project_root: str | Path) -> ProjectConfigPaths:
        project_path = Path(project_root)
        return ProjectConfigPaths(
            project_root=project_path,
            execution_cwd=project_path,
        )

    def fake_resolve_child_execution_cwd(**_kwargs: object) -> Path:
        return tmp_path

    def noop_write_params_json(*_args: object, **_kwargs: object) -> None:
        return None

    def run_coro(coro: Any) -> Any:
        return real_asyncio_run(coro)

    monkeypatch.setattr(execute_module, "_init_spawn", fake_init_spawn)
    monkeypatch.setattr(
        execute_module,
        "resolve_project_config_paths",
        fake_resolve_project_config_paths,
    )
    monkeypatch.setattr(
        execute_module,
        "resolve_child_execution_cwd",
        fake_resolve_child_execution_cwd,
    )
    monkeypatch.setattr(execute_module, "_write_params_json", noop_write_params_json)
    monkeypatch.setattr(
        execute_module,
        "launch_prepared_spawn",
        exploding_launch_prepared_spawn,
    )
    monkeypatch.setattr(execute_module.asyncio, "run", run_coro)
    monkeypatch.setattr(
        execute_module,
        "finalize_launch_failure_sync",
        fake_finalize_launch_failure_sync,
    )

    result = execute_spawn_blocking(
        payload=cast("Any", SimpleNamespace(desc="", work="", debug=False, stream=False)),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
        runtime=cast("Any", SimpleNamespace(project_root=tmp_path, sink=None)),
    )

    assert result.status == "failed"
    assert result.error == "execution_crash"
    assert result.exit_code == 1
    assert captured["runtime_root"] == runtime_root
    assert captured["project_root"] == tmp_path
    assert captured["spawn_id"] == spawn_id
    assert captured["error"] == "helper escaped"


@pytest.mark.asyncio
async def test_execute_existing_spawn_routes_through_launch_prepared_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.ops.spawn.execute as execute_module

    runtime_root = tmp_path / ".runtime"
    spawn_id = SpawnId("p1")
    _start_background_spawn_row(
        tmp_path=tmp_path,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
        model="stored-model",
        harness="codex",
    )

    captured: dict[str, object] = {}

    async def fake_launch_prepared_spawn(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    def fake_resolve_runtime_root(_project_root: Path) -> Path:
        return runtime_root

    def fake_build_runtime(
        _project_root: str,
        *,
        sink: object | None = None,
    ) -> SimpleNamespace:
        _ = sink
        return SimpleNamespace(
            harness_registry=SimpleNamespace(),
            artifacts=None,
        )

    monkeypatch.setattr(execute_module, "launch_prepared_spawn", fake_launch_prepared_spawn)
    monkeypatch.setattr(
        execute_module,
        "resolve_runtime_root",
        fake_resolve_runtime_root,
    )
    monkeypatch.setattr(
        execute_module,
        "build_runtime",
        fake_build_runtime,
    )

    result = await _execute_existing_spawn(
        spawn_id=spawn_id,
        project_paths=ProjectConfigPaths(project_root=tmp_path, execution_cwd=tmp_path),
        launch_request=_background_launch_request(
            tmp_path=tmp_path,
            prompt="run it",
            harness="codex",
            model="gpt-5.4",
        ),
    )

    assert result == 0
    assert cast("Any", captured["spawn"]).spawn_id == spawn_id
    assert cast("Any", captured["request"]).prompt == "run it"
    assert cast("Any", captured["request"]).harness == "codex"
    assert cast("Any", captured["project_paths"]).project_root == tmp_path
    assert captured["execution_cwd"] == tmp_path.as_posix()
