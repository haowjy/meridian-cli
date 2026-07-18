"""Foreground spawn execution output/report boundary tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import meridian.lib.ops.spawn.execute as execute_module
import meridian.lib.ops.spawn.execute_init as execute_init_module
from meridian.lib.config.settings import load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.lifecycle import LifecycleEvent, SpawnLifecycleService
from meridian.lib.core.sink import OutputSink
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.ops.runtime import (
    OperationRuntime,
    build_runtime_from_root_and_config,
    resolve_runtime_authority_for_write,
)
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.state import spawn_store, work_repository, work_store
from meridian.lib.state.paths import resolve_project_paths

if TYPE_CHECKING:
    import pytest

# qa-validated: spawn-return-report


class RecordingOutputSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def result(self, payload: Any) -> None:
        _ = payload

    def status(self, message: str) -> None:
        _ = message

    def warning(self, message: str) -> None:
        _ = message

    def error(self, message: str, exit_code: int = 1) -> None:
        _ = (message, exit_code)

    def heartbeat(self, message: str) -> None:
        _ = message

    def event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


class LifecycleEventHook:
    def __init__(self) -> None:
        self.event_types: list[str] = []

    def on_event(self, event: LifecycleEvent) -> None:
        self.event_types.append(event.event_type)


def _patch_lifecycle_service_with_hook(
    monkeypatch: pytest.MonkeyPatch,
    hook: LifecycleEventHook,
) -> None:
    original_factory = execute_init_module.build_spawn_lifecycle_service_from_roots

    def _service_with_hook(project_root_arg: Path, runtime_root: Path) -> SpawnLifecycleService:
        service = original_factory(project_root_arg, runtime_root)
        service.register_hook(hook)
        return service

    monkeypatch.setattr(
        execute_init_module,
        "build_spawn_lifecycle_service_from_roots",
        _service_with_hook,
    )


def _build_test_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sink: OutputSink | None = None,
) -> OperationRuntime:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    authority = resolve_runtime_authority_for_write(project_root)
    assert authority.runtime_root is not None
    config = load_config(project_root, authority=authority)
    return build_runtime_from_root_and_config(
        project_root,
        config,
        authority=authority,
        sink=sink,
    )


def test_execute_spawn_blocking_reads_report_and_does_not_print_running_preamble(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _build_test_runtime(tmp_path, monkeypatch)

    async def _fake_launch_prepared_spawn(**kwargs: object) -> int:
        spawn = cast("Any", kwargs["spawn"])
        runtime_root = Path(cast("Path", kwargs["runtime_root"]))
        spawn_id = str(spawn.spawn_id)
        report_path = runtime_root / "spawns" / spawn_id / "report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("fake report body\n", encoding="utf-8")
        history_path = runtime_root / "spawns" / spawn_id / "history.jsonl"
        history_path.write_text('{"event_type":"session.idle"}\n', encoding="utf-8")
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            "succeeded",
            0,
            origin="runner",
            duration_secs=1.25,
        )
        return 0

    monkeypatch.setattr(execute_module, "launch_prepared_spawn", _fake_launch_prepared_spawn)

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run"),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="codex",
            agent="coder",
        ),
        runtime=runtime,
    )

    captured = capsys.readouterr()
    assert '{"spawn_id":' not in captured.out
    assert '"status": "running"' not in captured.out
    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.report == "fake report body"
    assert result.duration_secs == 1.25
    assert result.format_text().endswith(
        "fake report body\n\nTranscript: meridian session log " + str(result.spawn_id)
    )


def test_execute_spawn_blocking_notifies_spawn_id_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _build_test_runtime(tmp_path, monkeypatch)
    notifications: list[str] = []

    async def _fake_launch_prepared_spawn(**kwargs: object) -> int:
        assert notifications == [str(cast("Any", kwargs["spawn"]).spawn_id)]
        spawn = cast("Any", kwargs["spawn"])
        runtime_root = Path(cast("Path", kwargs["runtime_root"]))
        spawn_store.finalize_spawn(
            runtime_root,
            str(spawn.spawn_id),
            "succeeded",
            0,
            origin="runner",
        )
        return 0

    monkeypatch.setattr(execute_module, "launch_prepared_spawn", _fake_launch_prepared_spawn)

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run"),
        request=SpawnRequest(prompt="run", model="gpt-5.4", harness="codex"),
        runtime=runtime,
        on_spawn_id=notifications.append,
    )

    assert result.status == "succeeded"
    assert notifications == [str(result.spawn_id)]


def test_execute_spawn_blocking_pre_init_failure_returns_failed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _build_test_runtime(tmp_path, monkeypatch)

    def _raise_project_paths(**kwargs: object) -> object:
        raise ValueError("harness binary not found")

    monkeypatch.setattr(execute_module, "resolve_project_config_paths", _raise_project_paths)

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run"),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="opencode",
            agent="browser-prober",
            task_cwd="/tmp/browser-work",
            task_cwd_source="explicit-task-dir",
            task_cwd_work_item="browser-investigation",
        ),
        runtime=runtime,
    )

    assert result.status == "failed"
    assert result.spawn_id is None
    assert result.error == "pre_init_failed"
    assert result.exit_code == 1
    assert result.model == "gpt-5.4"
    assert result.harness_id == "opencode"
    assert result.message is not None
    assert "harness binary not found" in result.message
    assert result.to_wire()["message"] == result.message
    assert result.to_wire()["model"] == "gpt-5.4"
    assert result.to_wire()["harness_id"] == "opencode"
    assert result.to_wire()["task_cwd_source"] == "explicit-task-dir"
    assert result.to_wire()["task_cwd_work_item"] == "browser-investigation"
    authority = resolve_runtime_authority_for_write(tmp_path / "repo")
    assert authority.runtime_root is not None
    assert not list((authority.runtime_root / "spawns").glob("*/state.json"))


def test_reserve_then_prepare_graceful_prep_failure_leaks_nothing_with_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = LifecycleEventHook()
    sink = RecordingOutputSink()
    runtime = _build_test_runtime(tmp_path, monkeypatch, sink=sink)
    project_root = tmp_path / "repo"
    project_state_dir = resolve_project_paths(project_root).root_dir
    authority = resolve_runtime_authority_for_write(project_root)
    assert authority.runtime_root is not None

    _patch_lifecycle_service_with_hook(monkeypatch, hook)

    def _raise_project_paths(**kwargs: object) -> object:
        raise ValueError("harness binary not found")

    monkeypatch.setattr(execute_module, "resolve_project_config_paths", _raise_project_paths)

    assert work_store.get_work_item(project_state_dir, "new-work-item") is None

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run", work="new-work-item"),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="codex",
        ),
        runtime=runtime,
        ctx=RuntimeContext(depth=1, spawn_id="p-parent"),
    )

    assert result.status == "failed"
    assert result.spawn_id is None
    assert result.error == "pre_init_failed"
    assert not list((authority.runtime_root / "spawns").glob("*/state.json"))
    assert work_store.get_work_item(project_state_dir, "new-work-item") is None
    assert hook.event_types == []
    assert [event.get("t") for event in sink.events] == []


def test_reserve_then_prepare_happy_path_announces_once_with_normalized_work_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = LifecycleEventHook()
    sink = RecordingOutputSink()
    runtime = _build_test_runtime(tmp_path, monkeypatch, sink=sink)
    project_root = tmp_path / "repo"
    project_state_dir = resolve_project_paths(project_root).root_dir
    authority = resolve_runtime_authority_for_write(project_root)
    assert authority.runtime_root is not None

    _patch_lifecycle_service_with_hook(monkeypatch, hook)

    async def _fake_launch_prepared_spawn(**kwargs: object) -> int:
        spawn = cast("Any", kwargs["spawn"])
        spawn_store.finalize_spawn(
            authority.runtime_root,
            str(spawn.spawn_id),
            "succeeded",
            0,
            origin="runner",
        )
        return 0

    monkeypatch.setattr(execute_module, "launch_prepared_spawn", _fake_launch_prepared_spawn)

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run", work="new-work-item"),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="codex",
            agent="coder",
        ),
        runtime=runtime,
        ctx=RuntimeContext(depth=1, spawn_id="p-parent"),
    )

    assert result.status == "succeeded"
    assert result.spawn_id is not None
    row = spawn_store.get_spawn(authority.runtime_root, result.spawn_id)
    assert row is not None
    assert row.work_id == "new-work-item"
    assert work_store.get_work_item(project_state_dir, "new-work-item") is not None
    assert hook.event_types == ["spawn.created"]
    start_events = [event for event in sink.events if event.get("t") == "meridian.spawn.start"]
    assert len(start_events) == 1
    assert start_events[0]["id"] == result.spawn_id


def test_reserve_then_prepare_background_happy_path_announces_once_with_normalized_work_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = LifecycleEventHook()
    sink = RecordingOutputSink()
    runtime = _build_test_runtime(tmp_path, monkeypatch, sink=sink)
    project_root = tmp_path / "repo"
    project_state_dir = resolve_project_paths(project_root).root_dir
    authority = resolve_runtime_authority_for_write(project_root)
    assert authority.runtime_root is not None

    _patch_lifecycle_service_with_hook(monkeypatch, hook)

    class _FakePopen:
        pid = 42424

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FakePopen())

    result = execute_module.execute_spawn_background(
        payload=SpawnCreateInput(prompt="run", work="new-work-item", background=True),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="codex",
            agent="coder",
        ),
        runtime=runtime,
        ctx=RuntimeContext(depth=1, spawn_id="p-parent"),
    )

    assert result.status == "running"
    assert result.background is True
    assert result.spawn_id is not None
    row = spawn_store.get_spawn(authority.runtime_root, result.spawn_id)
    assert row is not None
    assert row.work_id == "new-work-item"
    assert row.status == "running"
    assert work_store.get_work_item(project_state_dir, "new-work-item") is not None
    assert hook.event_types == ["spawn.created"]
    start_events = [event for event in sink.events if event.get("t") == "meridian.spawn.start"]
    assert len(start_events) == 1
    assert start_events[0]["id"] == result.spawn_id


def test_execute_spawn_blocking_persists_unified_work_id_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.core.context import RuntimeContext

    runtime = _build_test_runtime(tmp_path, monkeypatch)
    authority = resolve_runtime_authority_for_write(tmp_path / "repo")
    assert authority.runtime_root is not None

    async def _fake_launch_prepared_spawn(**kwargs: object) -> int:
        spawn = cast("Any", kwargs["spawn"])
        spawn_store.finalize_spawn(
            authority.runtime_root,
            str(spawn.spawn_id),
            "succeeded",
            0,
            origin="runner",
        )
        return 0

    monkeypatch.setattr(execute_module, "launch_prepared_spawn", _fake_launch_prepared_spawn)

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run", work="from-payload"),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="codex",
            task_cwd_work_item="from-request",
            work_id_hint="from-hint",
        ),
        runtime=runtime,
        ctx=RuntimeContext(work_id="ambient-work"),
    )

    assert result.status == "succeeded"
    assert result.spawn_id is not None
    row = spawn_store.get_spawn(authority.runtime_root, result.spawn_id)
    assert row is not None
    assert row.work_id == "from-request"


def test_reserve_then_prepare_materializes_context_from_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = LifecycleEventHook()
    sink = RecordingOutputSink()
    runtime = _build_test_runtime(tmp_path, monkeypatch, sink=sink)
    project_root = tmp_path / "repo"
    project_state_dir = resolve_project_paths(project_root).root_dir
    authority = resolve_runtime_authority_for_write(project_root)
    assert authority.runtime_root is not None

    _patch_lifecycle_service_with_hook(monkeypatch, hook)
    work_repository.create_work_item(project_state_dir, "source-work", "", None)

    async def _fake_launch_prepared_spawn(**kwargs: object) -> int:
        spawn = cast("Any", kwargs["spawn"])
        spawn_store.finalize_spawn(
            authority.runtime_root,
            str(spawn.spawn_id),
            "succeeded",
            0,
            origin="runner",
        )
        return 0

    monkeypatch.setattr(execute_module, "launch_prepared_spawn", _fake_launch_prepared_spawn)

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run", context_from=("p123",)),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="codex",
            agent="coder",
            context_from=("p123",),
            work_id_hint="source-work",
            task_cwd_work_item="source-work",
            inherited_context_work_id="source-work",
        ),
        runtime=runtime,
        ctx=RuntimeContext(depth=1, spawn_id="p-parent"),
    )

    assert result.status == "succeeded"
    assert result.spawn_id is not None
    row = spawn_store.get_spawn(authority.runtime_root, result.spawn_id)
    assert row is not None
    assert row.work_id == "source-work"
    assert work_store.get_work_item(project_state_dir, "source-work") is not None


def test_execute_spawn_background_pre_init_failure_returns_failed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _build_test_runtime(tmp_path, monkeypatch)

    def _raise_project_paths(**kwargs: object) -> object:
        raise PermissionError("permission denied resolving project")

    monkeypatch.setattr(execute_module, "resolve_project_config_paths", _raise_project_paths)

    result = execute_module.execute_spawn_background(
        payload=SpawnCreateInput(prompt="run", background=True),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="opencode",
            agent="browser-prober",
            task_cwd="/tmp/browser-work",
            task_cwd_source="explicit-task-dir",
            task_cwd_work_item="browser-investigation",
        ),
        runtime=runtime,
    )

    assert result.status == "failed"
    assert result.spawn_id is None
    assert result.error == "pre_init_failed"
    assert result.exit_code == 1
    assert result.model == "gpt-5.4"
    assert result.harness_id == "opencode"
    assert result.message is not None
    assert "permission denied resolving project" in result.message
    assert result.to_wire()["message"] == result.message
    assert result.to_wire()["model"] == "gpt-5.4"
    assert result.to_wire()["harness_id"] == "opencode"
    assert result.to_wire()["task_cwd_source"] == "explicit-task-dir"
    assert result.to_wire()["task_cwd_work_item"] == "browser-investigation"
    authority = resolve_runtime_authority_for_write(tmp_path / "repo")
    assert authority.runtime_root is not None
    assert not list((authority.runtime_root / "spawns").glob("*/state.json"))
