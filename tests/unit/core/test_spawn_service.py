"""Unit tests for SpawnApplicationService lifecycle seams."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import meridian.lib.core.telemetry as telemetry
from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_service import PreparedSpawn, SpawnApplicationService
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.state import spawn_store


@pytest.fixture
def mock_harness_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the harness registry to avoid real harness dependencies."""
    from unittest.mock import MagicMock

    from meridian.lib.harness.registry import HarnessRegistry

    mock_registry = MagicMock(spec=HarnessRegistry)
    mock_adapter = MagicMock()
    mock_adapter.id = MagicMock()
    mock_adapter.id.value = "codex"
    mock_adapter.capabilities = MagicMock()
    mock_adapter.capabilities.supports_native_agents = False
    mock_adapter.capabilities.supports_session_resume = False
    mock_adapter.capabilities.supports_session_fork = False
    mock_adapter.run_prompt_policy.return_value = MagicMock(skill_injection_mode="none")
    mock_adapter.preflight.return_value = MagicMock(
        expanded_passthrough_args=(),
        extra_env={},
    )
    mock_adapter.project_content.return_value = MagicMock(
        system_prompt="",
        user_turn_content="test prompt",
    )
    mock_adapter.seed_session.return_value = MagicMock(
        session_id=None,
        session_args=(),
    )
    mock_registry.get_subprocess_harness.return_value = mock_adapter
    monkeypatch.setattr(
        "meridian.lib.harness.registry.get_default_harness_registry",
        lambda: mock_registry,
    )
    return None


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[telemetry.LifecycleEvent] = []

    def on_event(self, event: telemetry.LifecycleEvent) -> None:
        self.events.append(event)


class _LifecycleHookCollector:
    def __init__(self) -> None:
        self.events: list[object] = []

    def on_event(self, event: object) -> None:
        self.events.append(event)


def _start_running_spawn(lifecycle: SpawnLifecycleService) -> SpawnId:
    return SpawnId(
        lifecycle.start(
            chat_id="chat-1",
            model="model-1",
            agent="coder",
            harness="codex",
            prompt="do the thing",
            status="running",
        )
    )


def _launch_runtime(project_root: Path, runtime_root: Path) -> LaunchRuntime:
    return LaunchRuntime(
        runtime_root=runtime_root.as_posix(),
        project_paths_project_root=project_root.as_posix(),
        project_paths_execution_cwd=project_root.as_posix(),
    )


def _mock_launch_context(
    *,
    spawn_id: str,
    child_cwd: Path,
    model: str = "gpt-5.4",
    harness: str = "codex",
    agent: str | None = "coder",
    prompt: str = "test prompt",
    work_id: str | None = None,
    env_overrides: dict[str, str] | None = None,
    system_prompt: str | None = None,
) -> SimpleNamespace:
    resolved_request = SimpleNamespace(
        model=model,
        harness=harness,
        agent=agent,
        prompt=prompt,
        skills=(),
        skill_paths=(),
        session=SimpleNamespace(requested_harness_session_id=None),
        agent_metadata={},
    )
    return SimpleNamespace(
        resolved_request=resolved_request,
        run_params=SimpleNamespace(appended_system_prompt=system_prompt),
        child_cwd=child_cwd,
        work_id=work_id,
        env_overrides=env_overrides or {"MERIDIAN_SPAWN_ID": spawn_id},
    )


@pytest.mark.asyncio
async def test_complete_spawn_returns_outcome_for_first_terminal_transition(
    tmp_path: Path,
) -> None:
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)

    outcome = await service.complete_spawn(
        spawn_id,
        "succeeded",
        0,
        origin="runner",
        duration_secs=1.5,
        total_cost_usd=0.25,
        input_tokens=10,
        output_tokens=20,
    )

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert outcome.wrote is True
    assert outcome.transitioned is True
    assert outcome.entered_finalizing is True
    assert outcome.already_terminal is False
    assert outcome.snapshot is not None
    assert outcome.snapshot.status == "succeeded"
    assert outcome.accepted is True
    assert record is not None
    assert record.status == "succeeded"
    assert record.exit_code == 0
    assert record.duration_secs == 1.5
    assert record.total_cost_usd == 0.25
    assert record.input_tokens == 10
    assert record.output_tokens == 20
    assert record.terminal_origin == "runner"


@pytest.mark.asyncio
async def test_complete_spawn_returns_missing_outcome_for_missing_spawn(
    tmp_path: Path,
) -> None:
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)

    outcome = await service.complete_spawn(
        SpawnId("p-missing"),
        "succeeded",
        0,
        origin="runner",
    )

    assert outcome.wrote is False
    assert outcome.transitioned is False
    assert outcome.entered_finalizing is False
    assert outcome.already_terminal is False
    assert outcome.snapshot is None
    assert outcome.spawn_id == SpawnId("p-missing")


@pytest.mark.asyncio
async def test_complete_spawn_returns_already_terminal_outcome_for_terminal_spawn(
    tmp_path: Path,
) -> None:
    hook = _LifecycleHookCollector()
    lifecycle = SpawnLifecycleService(tmp_path, hooks=[hook])
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)
    await service.complete_spawn(spawn_id, "succeeded", 0, origin="runner")
    hook.events.clear()

    outcome = await service.complete_spawn(spawn_id, "failed", 1, origin="cancel")

    assert outcome.wrote is False
    assert outcome.transitioned is False
    assert outcome.entered_finalizing is False
    assert outcome.already_terminal is True
    assert outcome.snapshot is not None
    assert outcome.snapshot.status == "succeeded"
    assert outcome.snapshot.terminal_origin == "runner"
    assert [getattr(event, "event_type", None) for event in hook.events] == []


@pytest.mark.asyncio
async def test_complete_spawn_replaces_reconciler_terminal(
    tmp_path: Path,
) -> None:
    """CR1: authoritative completion replaces a reconciler terminal."""
    hook = _LifecycleHookCollector()
    owner_lifecycle = SpawnLifecycleService(tmp_path)
    spawn_id = _start_running_spawn(owner_lifecycle)
    reconciler_lifecycle = SpawnLifecycleService(tmp_path)
    reconciler_lifecycle.finalize(
        str(spawn_id),
        "failed",
        1,
        origin="reconciler",
        error="orphaned",
    )
    lifecycle = SpawnLifecycleService(tmp_path, hooks=[hook])
    service = SpawnApplicationService(tmp_path, lifecycle)
    hook.events.clear()

    outcome = await service.complete_spawn(
        spawn_id,
        "succeeded",
        0,
        origin="runner",
        duration_secs=2.5,
    )

    assert outcome.wrote is True
    assert outcome.transitioned is False
    assert outcome.entered_finalizing is False
    assert outcome.already_terminal is True
    assert outcome.snapshot is not None
    assert outcome.snapshot.status == "succeeded"
    assert outcome.snapshot.exit_code == 0
    assert outcome.snapshot.terminal_origin == "runner"
    assert outcome.snapshot.duration_secs == 2.5
    assert [getattr(event, "event_type", None) for event in hook.events] == [
        "spawn.finalized"
    ]


@pytest.mark.asyncio
async def test_complete_spawn_rejects_authoritative_loser(
    tmp_path: Path,
) -> None:
    """CR1: later authoritative finalizer is rejected with unchanged snapshot."""
    hook = _LifecycleHookCollector()
    lifecycle = SpawnLifecycleService(tmp_path, hooks=[hook])
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)
    await service.complete_spawn(
        spawn_id,
        "succeeded",
        0,
        origin="runner",
        duration_secs=1.0,
    )
    hook.events.clear()

    outcome = await service.complete_spawn(
        spawn_id,
        "failed",
        1,
        origin="launcher",
        duration_secs=9.0,
        error="late launcher failure",
    )

    assert outcome.wrote is False
    assert outcome.transitioned is False
    assert outcome.entered_finalizing is False
    assert outcome.already_terminal is True
    assert outcome.snapshot is not None
    assert outcome.snapshot.status == "succeeded"
    assert outcome.snapshot.exit_code == 0
    assert outcome.snapshot.terminal_origin == "runner"
    assert outcome.snapshot.duration_secs == 1.0
    assert outcome.snapshot.error is None
    assert [getattr(event, "event_type", None) for event in hook.events] == []


@pytest.mark.asyncio
async def test_complete_spawn_on_queued_spawn_does_not_enter_finalizing(
    tmp_path: Path,
) -> None:
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = SpawnId(
        lifecycle.start(
            chat_id="chat-1",
            model="model-1",
            agent="coder",
            harness="codex",
            prompt="do the thing",
            status="queued",
        )
    )

    outcome = await service.complete_spawn(spawn_id, "failed", 1, origin="launcher")

    assert outcome.wrote is True
    assert outcome.transitioned is True
    assert outcome.entered_finalizing is False
    assert outcome.already_terminal is False
    assert outcome.snapshot is not None
    assert outcome.snapshot.status == "failed"


@pytest.mark.asyncio
async def test_get_spawn_failure_returns_failure_sentinel(tmp_path: Path) -> None:
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)

    await service.complete_spawn(spawn_id, "failed", 2, origin="runner")

    failure = service.get_spawn_failure(spawn_id)
    assert failure is not None
    assert failure.spawn_id == str(spawn_id)
    assert failure.exit_code == 2
    assert failure.reason == "runner"


@pytest.mark.asyncio
async def test_get_spawn_failure_ignores_sentinel_when_spawn_is_not_failed(
    tmp_path: Path,
) -> None:
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)

    SpawnLifecycleService(tmp_path).finalize(str(spawn_id), "failed", 2, origin="reconciler")
    SpawnLifecycleService(tmp_path).finalize(str(spawn_id), "succeeded", 0, origin="launcher")

    assert service.get_spawn_failure(spawn_id) is None


@pytest.mark.asyncio
async def test_complete_spawn_returns_false_after_terminal_transition(
    tmp_path: Path,
) -> None:
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)

    first = await service.complete_spawn(
        spawn_id,
        "succeeded",
        0,
        origin="runner",
        duration_secs=1.5,
        total_cost_usd=0.25,
        input_tokens=10,
        output_tokens=20,
    )
    second = await service.complete_spawn(
        spawn_id,
        "failed",
        1,
        origin="cancel",
        duration_secs=9.9,
        total_cost_usd=4.25,
        input_tokens=999,
        output_tokens=888,
    )

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert first.wrote is True
    assert first.transitioned is True
    assert second.wrote is False
    assert second.transitioned is False
    assert second.already_terminal is True
    assert record is not None
    assert record.status == "succeeded"
    assert record.exit_code == 0
    assert record.terminal_origin == "runner"
    assert record.duration_secs == 1.5
    assert record.total_cost_usd == 0.25
    assert record.input_tokens == 10
    assert record.output_tokens == 20


@pytest.mark.asyncio
async def test_complete_spawn_serializes_concurrent_terminal_attempts(
    tmp_path: Path,
) -> None:
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)

    results = await asyncio.gather(
        service.complete_spawn(spawn_id, "succeeded", 0, origin="runner"),
        service.complete_spawn(spawn_id, "cancelled", 130, origin="cancel"),
    )

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert sorted(result.transitioned for result in results) == [False, True]
    assert all(isinstance(result.wrote, bool) for result in results)
    assert all(isinstance(result.transitioned, bool) for result in results)
    assert record is not None
    assert record.status in {"succeeded", "cancelled"}


@pytest.mark.asyncio
async def test_prepare_spawn_persists_trimmed_resolved_metadata(
    tmp_path: Path,
    mock_harness_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEAM-2.1/2.2/2.3: persisted row uses resolved metadata, not placeholders."""
    from meridian.lib.harness.registry import get_default_harness_registry

    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime_root = tmp_path

    def build_launch_context(**kwargs: object) -> SimpleNamespace:
        return _mock_launch_context(
            spawn_id=str(kwargs["spawn_id"]),
            child_cwd=project_root,
            model="  gpt-5.4  ",
            harness="  codex  ",
            agent="  coder  ",
            prompt="resolved prompt",
        )

    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        build_launch_context,
    )

    lifecycle = SpawnLifecycleService(runtime_root)
    service = SpawnApplicationService(runtime_root, lifecycle)
    prepared = await service.prepare_spawn(
        request=SpawnRequest(prompt="request prompt", harness="codex", model="alias"),
        runtime=_launch_runtime(project_root, runtime_root),
        harness_registry=get_default_harness_registry(),
        chat_id="chat-1",
        initial_status="running",
    )

    record = spawn_store.get_spawn(runtime_root, prepared.spawn_id)
    assert isinstance(prepared, PreparedSpawn)
    assert prepared.resolved_model == "gpt-5.4"
    assert prepared.resolved_harness == "codex"
    assert prepared.resolved_agent == "coder"
    assert record is not None
    assert record.model == "gpt-5.4"
    assert record.harness == "codex"
    assert record.agent == "coder"
    assert record.model != "unknown"


@pytest.mark.asyncio
async def test_prepare_spawn_persists_empty_resolved_model_truthfully(
    tmp_path: Path,
    mock_harness_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty resolved model is valid and persists as empty, not as a sentinel."""
    from meridian.lib.harness.registry import get_default_harness_registry

    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime_root = tmp_path

    def build_launch_context(**kwargs: object) -> SimpleNamespace:
        return _mock_launch_context(
            spawn_id=str(kwargs["spawn_id"]),
            child_cwd=project_root,
            model="",
            harness="codex",
            agent="coder",
            prompt="resolved prompt",
        )

    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        build_launch_context,
    )

    lifecycle = SpawnLifecycleService(runtime_root)
    service = SpawnApplicationService(runtime_root, lifecycle)
    prepared = await service.prepare_spawn(
        request=SpawnRequest(prompt="request prompt", harness="codex"),
        runtime=_launch_runtime(project_root, runtime_root),
        harness_registry=get_default_harness_registry(),
        chat_id="chat-1",
        initial_status="running",
    )

    record = spawn_store.get_spawn(runtime_root, prepared.spawn_id)
    assert prepared.resolved_model == ""
    assert record is not None
    assert record.model == ""
    assert record.model != "unknown"


@pytest.mark.asyncio
async def test_prepare_spawn_persists_concrete_resolved_model(
    tmp_path: Path,
    mock_harness_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concrete resolved model still persists after removing the empty sentinel."""
    from meridian.lib.harness.registry import get_default_harness_registry

    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime_root = tmp_path

    def build_launch_context(**kwargs: object) -> SimpleNamespace:
        return _mock_launch_context(
            spawn_id=str(kwargs["spawn_id"]),
            child_cwd=project_root,
            model="gpt-5.4",
            harness="codex",
            agent="coder",
            prompt="resolved prompt",
        )

    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        build_launch_context,
    )

    lifecycle = SpawnLifecycleService(runtime_root)
    service = SpawnApplicationService(runtime_root, lifecycle)
    prepared = await service.prepare_spawn(
        request=SpawnRequest(prompt="request prompt", harness="codex"),
        runtime=_launch_runtime(project_root, runtime_root),
        harness_registry=get_default_harness_registry(),
        chat_id="chat-1",
        initial_status="running",
    )

    record = spawn_store.get_spawn(runtime_root, prepared.spawn_id)
    assert prepared.resolved_model == "gpt-5.4"
    assert record is not None
    assert record.model == "gpt-5.4"


@pytest.mark.asyncio
async def test_prepare_spawn_projects_connection_config_from_launch_context(
    tmp_path: Path,
    mock_harness_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEAM-3.1/3.2/3.3/3.4: PreparedSpawn projects prompt/cwd/env/system."""
    from meridian.lib.harness.registry import get_default_harness_registry

    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime_root = tmp_path

    def build_launch_context(**kwargs: object) -> SimpleNamespace:
        spawn_id = str(kwargs["spawn_id"])
        return _mock_launch_context(
            spawn_id=spawn_id,
            child_cwd=project_root,
            model="gpt-5.4",
            harness="codex",
            agent="coder",
            prompt="resolved prompt",
            work_id="work-123",
            env_overrides={
                "MERIDIAN_SPAWN_ID": spawn_id,
                "MERIDIAN_ACTIVE_WORK_ID": "work-123",
                "EXTRA_FLAG": "1",
            },
            system_prompt="system from profile",
        )

    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        build_launch_context,
    )

    lifecycle = SpawnLifecycleService(runtime_root)
    service = SpawnApplicationService(runtime_root, lifecycle)
    prepared = await service.prepare_spawn(
        request=SpawnRequest(prompt="request prompt", harness="codex"),
        runtime=_launch_runtime(project_root, runtime_root),
        harness_registry=get_default_harness_registry(),
        chat_id="chat-1",
        initial_status="running",
    )

    assert prepared.connection_config.prompt == "resolved prompt"
    assert prepared.connection_config.project_root == project_root
    assert prepared.connection_config.env_overrides == {
        "MERIDIAN_SPAWN_ID": str(prepared.spawn_id),
        "MERIDIAN_ACTIVE_WORK_ID": "work-123",
        "EXTRA_FLAG": "1",
    }
    assert prepared.connection_config.system == "system from profile"
    assert prepared.work_id == "work-123"


@pytest.mark.asyncio
async def test_prepare_spawn_allocates_distinct_ids_under_concurrency(
    tmp_path: Path,
    mock_harness_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEAM-ID.1: concurrent prepare_spawn calls persist distinct spawn rows."""
    from meridian.lib.harness.registry import get_default_harness_registry

    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime_root = tmp_path
    barrier = threading.Barrier(2)

    def build_launch_context(**kwargs: object) -> SimpleNamespace:
        spawn_id = str(kwargs["spawn_id"])
        request = kwargs["request"]
        assert isinstance(request, SpawnRequest)
        if spawn_id.startswith("pending-"):
            barrier.wait(timeout=2)
        return _mock_launch_context(
            spawn_id=spawn_id,
            child_cwd=project_root,
            prompt=request.prompt,
        )

    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        build_launch_context,
    )

    lifecycle = SpawnLifecycleService(runtime_root)
    service = SpawnApplicationService(runtime_root, lifecycle)
    runtime = _launch_runtime(project_root, runtime_root)
    registry = get_default_harness_registry()

    first, second = await asyncio.gather(
        service.prepare_spawn(
            request=SpawnRequest(prompt="first prompt", harness="codex"),
            runtime=runtime,
            harness_registry=registry,
            chat_id="chat-1",
            initial_status="running",
        ),
        service.prepare_spawn(
            request=SpawnRequest(prompt="second prompt", harness="codex"),
            runtime=runtime,
            harness_registry=registry,
            chat_id="chat-1",
            initial_status="running",
        ),
    )

    assert first.spawn_id != second.spawn_id

    first_record = spawn_store.get_spawn(runtime_root, first.spawn_id)
    second_record = spawn_store.get_spawn(runtime_root, second.spawn_id)
    assert first_record is not None
    assert second_record is not None
    assert {first_record.id, second_record.id} == {first.spawn_id, second.spawn_id}
    assert {first_record.prompt, second_record.prompt} == {"first prompt", "second prompt"}


@pytest.mark.asyncio
async def test_prepare_spawn_no_row_on_resolution_failure(
    tmp_path: Path,
    mock_harness_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEAM-1: no spawn row exists when the first launch composition fails."""
    from meridian.lib.harness.registry import get_default_harness_registry

    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime_root = tmp_path

    def failing_build_launch_context(**kwargs: object) -> None:
        raise ValueError("Bad model alias")

    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        failing_build_launch_context,
    )

    lifecycle = SpawnLifecycleService(runtime_root)
    service = SpawnApplicationService(runtime_root, lifecycle)

    assert spawn_store.list_spawns(runtime_root) == []

    with pytest.raises(ValueError, match="Bad model alias"):
        await service.prepare_spawn(
            request=SpawnRequest(prompt="test prompt", harness="codex", model="bad-model"),
            runtime=_launch_runtime(project_root, runtime_root),
            harness_registry=get_default_harness_registry(),
            chat_id="chat-1",
        )

    assert spawn_store.list_spawns(runtime_root) == []


@pytest.mark.asyncio
async def test_prepare_spawn_rolls_back_row_if_rebuild_with_final_id_fails(
    tmp_path: Path,
    mock_harness_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEAM-4.1 adversarial case: second build failure should not leak a row."""
    from meridian.lib.harness.registry import get_default_harness_registry

    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime_root = tmp_path
    calls = 0

    def build_launch_context(**kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        spawn_id = str(kwargs["spawn_id"])
        if calls == 1:
            return _mock_launch_context(spawn_id=spawn_id, child_cwd=project_root)
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        build_launch_context,
    )

    lifecycle = SpawnLifecycleService(runtime_root)
    service = SpawnApplicationService(runtime_root, lifecycle)

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await service.prepare_spawn(
            request=SpawnRequest(prompt="test prompt", harness="codex"),
            runtime=_launch_runtime(project_root, runtime_root),
            harness_registry=get_default_harness_registry(),
            chat_id="chat-1",
        )

    assert spawn_store.list_spawns(runtime_root) == []


@pytest.mark.asyncio
async def test_prepare_spawn_rebuild_failure_emits_no_lifecycle_events(
    tmp_path: Path,
    mock_harness_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEAM-4.1: rollback path must not leak ghost spawn lifecycle emissions."""
    from meridian.lib.harness.registry import get_default_harness_registry

    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime_root = tmp_path
    hook = _LifecycleHookCollector()
    calls = 0

    def build_launch_context(**kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        spawn_id = str(kwargs["spawn_id"])
        if calls == 1:
            return _mock_launch_context(
                spawn_id=spawn_id,
                child_cwd=project_root,
                prompt="first pass prompt",
            )
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.build_launch_context",
        build_launch_context,
    )

    lifecycle = SpawnLifecycleService(runtime_root, hooks=[hook])
    service = SpawnApplicationService(runtime_root, lifecycle)

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await service.prepare_spawn(
            request=SpawnRequest(prompt="test prompt", harness="codex"),
            runtime=_launch_runtime(project_root, runtime_root),
            harness_registry=get_default_harness_registry(),
            chat_id="chat-1",
            initial_status="running",
        )

    assert spawn_store.list_spawns(runtime_root) == []
    assert [getattr(event, "event_type", None) for event in hook.events] == []


@pytest.mark.asyncio
async def test_archive_rejects_non_terminal_spawn(tmp_path: Path) -> None:
    """SEAM-5.1: archive refuses running spawns."""
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)

    with pytest.raises(ValueError, match="Cannot archive non-terminal spawn"):
        await service.archive(spawn_id)


@pytest.mark.asyncio
async def test_archive_is_idempotent_and_emits_spawn_archived_once(tmp_path: Path) -> None:
    """SEAM-5.2/5.3: archive is idempotent and emits exactly one event."""
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)
    collector = _EventCollector()
    service.register_observer(collector)

    await service.complete_spawn(spawn_id, "succeeded", 0, origin="runner")

    first = await service.archive(spawn_id)
    second = await service.archive(spawn_id)

    archived_events = [event for event in collector.events if event.event == "spawn.archived"]
    assert first is True
    assert second is False
    assert len(archived_events) == 1
    assert archived_events[0].spawn_id == str(spawn_id)
    assert archived_events[0].payload == {"archived": True}


@pytest.mark.asyncio
async def test_archive_concurrent_callers_emit_spawn_archived_once(tmp_path: Path) -> None:
    """SEAM-5.2/5.3: concurrent archive callers still produce one success and one event."""
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)
    collector = _EventCollector()
    service.register_observer(collector)

    await service.complete_spawn(spawn_id, "succeeded", 0, origin="runner")

    results = await asyncio.gather(
        service.archive(spawn_id),
        service.archive(spawn_id),
    )

    archived_events = [event for event in collector.events if event.event == "spawn.archived"]
    assert sorted(results) == [False, True]
    assert len(archived_events) == 1
    assert archived_events[0].spawn_id == str(spawn_id)
    assert archived_events[0].payload == {"archived": True}


def test_update_metadata_persists_changes_emits_event_and_preserves_status(
    tmp_path: Path,
) -> None:
    """SEAM-6.1/6.2: metadata updates persist and do not change lifecycle state."""
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)
    collector = _EventCollector()
    service.register_observer(collector)

    execution_cwd = str(tmp_path / "child")
    service.update_metadata(
        spawn_id,
        execution_cwd=execution_cwd,
        desc="updated desc",
        work_id="work-123",
        harness_session_id="session-123",
        error="boom",
    )

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    updated_events = [event for event in collector.events if event.event == "spawn.updated"]
    assert record is not None
    assert record.status == "running"
    assert record.execution_cwd == execution_cwd
    assert record.desc == "updated desc"
    assert record.work_id == "work-123"
    assert record.harness_session_id == "session-123"
    assert record.error == "boom"
    assert len(updated_events) == 1
    assert updated_events[0].payload == {
        "launch_mode": None,
        "worker_pid": None,
        "runner_pid": None,
        "harness_session_id": "session-123",
        "execution_cwd": execution_cwd,
        "claude_config_dir": None,
        "error": "boom",
        "desc": "updated desc",
        "work_id": "work-123",
    }


def test_update_metadata_partial_updates_preserve_omitted_fields_and_status(
    tmp_path: Path,
) -> None:
    """SEAM-6.1/6.2: partial metadata updates preserve prior values and emit per write."""
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)
    collector = _EventCollector()
    service.register_observer(collector)

    first_execution_cwd = str(tmp_path / "child-1")
    second_execution_cwd = str(tmp_path / "child-2")

    service.update_metadata(
        spawn_id,
        execution_cwd=first_execution_cwd,
        desc="first desc",
        work_id="work-123",
    )
    after_first = spawn_store.get_spawn(tmp_path, spawn_id)

    service.update_metadata(
        spawn_id,
        harness_session_id="session-123",
        error="boom",
    )
    after_second = spawn_store.get_spawn(tmp_path, spawn_id)

    service.update_metadata(
        spawn_id,
        execution_cwd=second_execution_cwd,
    )
    after_third = spawn_store.get_spawn(tmp_path, spawn_id)

    updated_events = [event for event in collector.events if event.event == "spawn.updated"]
    assert after_first is not None
    assert after_first.status == "running"
    assert after_first.execution_cwd == first_execution_cwd
    assert after_first.desc == "first desc"
    assert after_first.work_id == "work-123"
    assert after_first.harness_session_id is None
    assert after_first.error is None

    assert after_second is not None
    assert after_second.status == "running"
    assert after_second.execution_cwd == first_execution_cwd
    assert after_second.desc == "first desc"
    assert after_second.work_id == "work-123"
    assert after_second.harness_session_id == "session-123"
    assert after_second.error == "boom"

    assert after_third is not None
    assert after_third.status == "running"
    assert after_third.execution_cwd == second_execution_cwd
    assert after_third.desc == "first desc"
    assert after_third.work_id == "work-123"
    assert after_third.harness_session_id == "session-123"
    assert after_third.error == "boom"

    assert len(updated_events) == 3
    assert updated_events[0].payload == {
        "launch_mode": None,
        "worker_pid": None,
        "runner_pid": None,
        "harness_session_id": None,
        "execution_cwd": first_execution_cwd,
        "claude_config_dir": None,
        "error": None,
        "desc": "first desc",
        "work_id": "work-123",
    }
    assert updated_events[1].payload == {
        "launch_mode": None,
        "worker_pid": None,
        "runner_pid": None,
        "harness_session_id": "session-123",
        "execution_cwd": None,
        "claude_config_dir": None,
        "error": "boom",
        "desc": None,
        "work_id": None,
    }
    assert updated_events[2].payload == {
        "launch_mode": None,
        "worker_pid": None,
        "runner_pid": None,
        "harness_session_id": None,
        "execution_cwd": second_execution_cwd,
        "claude_config_dir": None,
        "error": None,
        "desc": None,
        "work_id": None,
    }


def test_update_metadata_is_noop_when_all_fields_are_none(tmp_path: Path) -> None:
    """SEAM-6.2: empty metadata update does nothing."""
    lifecycle = SpawnLifecycleService(tmp_path)
    service = SpawnApplicationService(tmp_path, lifecycle)
    spawn_id = _start_running_spawn(lifecycle)
    collector = _EventCollector()
    service.register_observer(collector)

    before = spawn_store.get_spawn(tmp_path, spawn_id)
    service.update_metadata(spawn_id)
    after = spawn_store.get_spawn(tmp_path, spawn_id)

    assert before == after
    assert collector.events == []
