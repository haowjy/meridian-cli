# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Streaming runner retry policy and resident deadline behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.request import RetryPolicy
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_runtime_root
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _build_opencode_request,
    _build_request,
    _CloseWithoutTerminalOpenCodeConnection,
    _execute_with_context,
    _FakeControlSocketServer,
    _pi_extension_projection_fixture,
    _ResidentDeadlineConnection,
    _RetryableOpenCodeConnection,
    streaming_runner_module,
)
from tests.support.fakes import FakeClock, FakeHeartbeat

_pi_extension_projection_fixture = _pi_extension_projection_fixture

@pytest.mark.asyncio
async def test_execute_with_streaming_finalizes_resident_deadline_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _ResidentDeadlineConnection.starts = 0
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ResidentDeadlineConnection,
    )
    monkeypatch.setattr(
        streaming_runner_module,
        "resolve_resident_deadline_seconds",
        lambda *, config_snapshot: 0.01,
    )
    monkeypatch.setattr(
        streaming_runner_module,
        "resolve_resident_poll_seconds",
        lambda *, config_snapshot: 0.001,
    )

    parent_id = SpawnId("r-resident-deadline")
    run = Spawn(
        spawn_id=parent_id,
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-resident-deadline",
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=parent_id,
        launch_mode="foreground",
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-resident-deadline-child",
        parent_id=str(parent_id),
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt="child",
        spawn_id=SpawnId("r-resident-deadline-child"),
        launch_mode="background",
        status="running",
    )
    request = _build_request().model_copy(
        update={"retry": RetryPolicy(max_attempts=3, backoff_secs=0.0)}
    )
    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=request,
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=fake_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, parent_id)
    assert exit_code == 1
    assert _ResidentDeadlineConnection.starts == 1
    assert row is not None
    assert row.status == "timed_out"
    assert row.exit_code == 1
    assert row.error == "resident_deadline_expired"



@pytest.mark.asyncio
async def test_execute_with_streaming_does_not_retry_authoritative_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _RetryableOpenCodeConnection.starts = 0
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _RetryableOpenCodeConnection,
    )

    run = Spawn(
        spawn_id=SpawnId("r-opencode-retryable"),
        prompt="hello",
        model=ModelId("gpt-5.4"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-opencode-retryable",
        model=str(run.model),
        agent="",
        harness=HarnessId.OPENCODE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    request = _build_opencode_request().model_copy(
        update={"retry": RetryPolicy(max_attempts=2, backoff_secs=0.0)}
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=request,
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=fake_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code == 1
    assert _RetryableOpenCodeConnection.starts == 1
    assert row is not None
    assert row.status == "failed"
    assert row.exit_code == 1
    assert row.error == "connection reset by peer"


@pytest.mark.asyncio
async def test_execute_with_streaming_retries_single_turn_close_without_terminal_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _CloseWithoutTerminalOpenCodeConnection.starts = 0
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: (
            _CloseWithoutTerminalOpenCodeConnection
        ),
    )

    run = Spawn(
        spawn_id=SpawnId("r-opencode-close-without-terminal"),
        prompt="hello",
        model=ModelId("gpt-5.4"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-opencode-close-without-terminal",
        model=str(run.model),
        agent="",
        harness=HarnessId.OPENCODE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    request = _build_opencode_request().model_copy(
        update={"retry": RetryPolicy(max_attempts=2, backoff_secs=0.0)}
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=request,
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=fake_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code == 0
    assert _CloseWithoutTerminalOpenCodeConnection.starts == 2
    assert row is not None
    assert row.status == "succeeded"
    assert row.exit_code == 0

