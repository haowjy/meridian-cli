"""Mars call budget: one compose per blocking spawn create+execute handoff."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.config.project_paths import resolve_project_config_paths
from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.composition_spawn import bind_spawn_launch_context
from meridian.lib.launch.context import RuntimeBindings
from meridian.lib.launch.plan import build_spawn_mars_runtime
from meridian.lib.launch.request import LaunchArgvIntent
from meridian.lib.ops.runtime import build_runtime
from meridian.lib.ops.spawn.execute_runner import _prepare_execution_handoff
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload
from meridian.lib.state import session_store, spawn_store
from tests.support.executables import prepend_fake_executables
from tests.support.launch import stub_bundle_request_and_resolve


def _seed_project(tmp_path: Path) -> Path:
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_blocking_spawn_compose_once_then_bind_only_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    captured = stub_bundle_request_and_resolve(
        monkeypatch,
        model="openai-codex/gpt-5.4-mini",
        harness=HarnessId.PI,
        harness_model="openai-codex/gpt-5.4-mini",
    )
    runtime = build_runtime(project_root)
    runtime_root = project_root / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)

    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="hi",
            model="openai-codex/gpt-5.4-mini",
            harness="pi",
            project_root=str(project_root),
        ),
        runtime=runtime,
    )
    assert len(captured) == 1

    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=runtime_root,
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
    )
    project_paths = resolve_project_config_paths(project_root=project_root)
    spawn = Spawn(
        spawn_id=SpawnId("p1"),
        prompt="hi",
        model=ModelId("openai-codex/gpt-5.4-mini"),
        status="running",
    )
    parent_chat_id = session_store.start_session(
        runtime_root,
        harness="claude",
        harness_session_id="parent-session",
        model="claude-sonnet-4-5",
        chat_id="c-parent",
        kind="primary",
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn.spawn_id,
        chat_id=parent_chat_id,
        owner_chat_id=parent_chat_id,
        parent_id="p-parent",
        model="openai-codex/gpt-5.4-mini",
        agent="",
        harness="pi",
        prompt="hi",
        harness_session_id="",
    )

    handoff = None
    try:
        handoff = await _prepare_execution_handoff(
            spawn=spawn,
            request=artifacts.request,
            runtime_request=launch_runtime,
            runtime=runtime,
            runtime_root=runtime_root,
            project_paths=project_paths,
            spawn_record=None,
            execution_cwd=project_root.as_posix(),
            work_id=None,
            ctx=None,
            prepared=artifacts.prepared,
        )
        assert len(captured) == 1
        assert handoff.launch_context.binding.spec.model == "openai-codex/gpt-5.4-mini"

        row = spawn_store.get_spawn(runtime_root, spawn.spawn_id)
        assert row is not None
        assert row.chat_id == handoff.session_context.chat_id
        assert row.owner_chat_id == parent_chat_id
        assert row.chat_id != parent_chat_id
        session_record = session_store.get_session_record(
            runtime_root,
            handoff.session_context.chat_id,
        )
        assert session_record is not None
        assert session_record.spawn_id == str(spawn.spawn_id)
    finally:
        if handoff is not None:
            handoff.session_exit_stack.close()
        session_store.stop_session(runtime_root, parent_chat_id)


def test_bind_spawn_launch_context_does_not_call_mars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    prepend_fake_executables(monkeypatch, tmp_path, "cursor")
    captured = stub_bundle_request_and_resolve(
        monkeypatch,
        model="opus47",
        harness=HarnessId.CURSOR,
        harness_model="claude-opus-4-7-thinking-high",
    )
    runtime = build_runtime(project_root)
    runtime_root = project_root / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)

    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="hi",
            model="opus47",
            harness="cursor",
            project_root=str(project_root),
        ),
        runtime=runtime,
    )
    assert len(captured) == 1

    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=runtime_root,
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
    )
    bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(
            spawn_id="p2",
            dry_run=False,
        ),
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )
    assert len(captured) == 1
