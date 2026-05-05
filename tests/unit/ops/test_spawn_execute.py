from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.config.project_paths import ProjectConfigPaths
from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.core.types import ModelId, SpawnId
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
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.launch.run_inputs import ResolvedRunInputs
from meridian.lib.ops.spawn.execute import (
    BackgroundWorkerLaunchRequest,
    _execute_existing_spawn,
    _SessionExecutionContext,
    _write_params_json,
    execute_spawn_blocking,
    launch_prepared_spawn,
)
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.repository import FileSpawnRepository


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
        return SimpleNamespace(resolved_request=kwargs["request"])

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
    events = FileSpawnRepository(RuntimePaths.from_root_dir(runtime_root)).read_events()
    finalize_events = [
        event
        for event in events
        if event.event == "finalize" and event.id == str(spawn_id)
    ]
    assert result == 1
    assert record is not None
    assert record.status == "failed"
    assert record.terminal_origin == "launch_failure"
    assert record.error == "boom before runner entry"
    assert len(finalize_events) == 1
    assert finalize_events[0].origin == "launch_failure"


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

    monkeypatch.setattr(execute_module, "_init_spawn", fake_init_spawn)
    monkeypatch.setattr(
        execute_module,
        "resolve_project_config_paths",
        lambda *, project_root: ProjectConfigPaths(
            project_root=Path(project_root),
            execution_cwd=Path(project_root),
        ),
    )
    monkeypatch.setattr(execute_module, "resolve_child_execution_cwd", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(execute_module, "_write_params_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execute_module, "launch_prepared_spawn", fake_launch_prepared_spawn)
    monkeypatch.setattr(execute_module.asyncio, "run", lambda coro: real_asyncio_run(coro))
    monkeypatch.setattr(
        execute_module,
        "read_spawn_row",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="succeeded",
            duration_secs=1.25,
            input_tokens=3,
            output_tokens=5,
        ),
    )

    result = execute_spawn_blocking(
        payload=cast("Any", SimpleNamespace(desc="", work="", debug=False, stream=False)),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
        runtime=cast("Any", SimpleNamespace(project_root=tmp_path, sink=None)),
    )

    assert result.status == "succeeded"
    assert captured["spawn"].spawn_id == spawn_id
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

    def fake_complete_spawn_sync(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(execute_module, "_init_spawn", fake_init_spawn)
    monkeypatch.setattr(
        execute_module,
        "resolve_project_config_paths",
        lambda *, project_root: ProjectConfigPaths(
            project_root=Path(project_root),
            execution_cwd=Path(project_root),
        ),
    )
    monkeypatch.setattr(execute_module, "resolve_child_execution_cwd", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(execute_module, "_write_params_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        execute_module,
        "launch_prepared_spawn",
        exploding_launch_prepared_spawn,
    )
    monkeypatch.setattr(execute_module.asyncio, "run", lambda coro: real_asyncio_run(coro))
    monkeypatch.setattr(
        execute_module,
        "create_lifecycle_service",
        lambda *_args, **_kwargs: "lifecycle-service",
    )
    monkeypatch.setattr(execute_module, "_complete_spawn_sync", fake_complete_spawn_sync)

    result = execute_spawn_blocking(
        payload=cast("Any", SimpleNamespace(desc="", work="", debug=False, stream=False)),
        request=SpawnRequest(prompt="run it", model="gpt-5.4", harness="codex"),
        runtime=cast("Any", SimpleNamespace(project_root=tmp_path, sink=None)),
    )

    assert result.status == "failed"
    assert result.error == "execution_crash"
    assert result.exit_code == 1
    assert captured["runtime_root"] == runtime_root
    assert captured["lifecycle_service"] == "lifecycle-service"
    assert captured["spawn_id"] == spawn_id
    assert captured["status"] == "failed"
    assert captured["exit_code"] == 1
    assert captured["origin"] == "launch_failure"
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

    monkeypatch.setattr(execute_module, "launch_prepared_spawn", fake_launch_prepared_spawn)
    monkeypatch.setattr(
        execute_module,
        "resolve_runtime_root",
        lambda _project_root: runtime_root,
    )
    monkeypatch.setattr(
        execute_module,
        "build_runtime",
        lambda _project_root, *, sink=None: SimpleNamespace(
            harness_registry=SimpleNamespace(),
            artifacts=None,
        ),
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
    assert captured["spawn"].spawn_id == spawn_id
    assert cast("Any", captured["request"]).prompt == "run it"
    assert cast("Any", captured["request"]).harness == "codex"
    assert cast("Any", captured["project_paths"]).project_root == tmp_path
    assert captured["execution_cwd"] == tmp_path.as_posix()
