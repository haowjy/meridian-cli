# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Streaming runner retry policy and resident deadline behavior."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.domain import Spawn
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.request import RetryPolicy
from meridian.lib.ops.runtime import build_runtime_from_root_and_config
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.streaming import pi_drain as pi_drain_module
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _build_opencode_request,
    _build_request,
    _execute_with_context,
    _FakeControlSocketServer,
    _pi_extension_projection_fixture,
    _ResidentDeadlineConnection,
    _ResidentRearmRetryConnection,
    _ScriptedRetryOpenCodeConnection,
    _TimeoutAbortPiConnection,
    streaming_runner_module,
)
from tests.support.fakes import FakeClock, FakeHeartbeat
from tests.support.launch import FakeBundleResult

_pi_extension_projection_fixture = _pi_extension_projection_fixture


@pytest.mark.parametrize("timeout_source", ["cli", "env"])
@pytest.mark.asyncio
async def test_execute_with_streaming_attempt_timeout_survives_pi_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout_source: str,
) -> None:
    async def _abort_tail_exit_failure(
        _coordinator: object, _recorded_outcome: object
    ) -> object:
        raise RuntimeError("Pi abort tail failed while classifying stream exit")

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        pi_drain_module.PiDrainCoordinator,
        "handle_stream_exit",
        _abort_tail_exit_failure,
    )
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _TimeoutAbortPiConnection,
    )
    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: FakeBundleResult(
            model="pi-test-model",
            model_token="pi-test-model",
            harness=HarnessId.PI,
            harness_model="pi-test-model",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "bundle", "harness_source": "cli"},
        ),
    )
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("MERIDIAN_TIMEOUT", raising=False)
    if timeout_source == "env":
        monkeypatch.setenv("MERIDIAN_TIMEOUT", "0.001")
    else:
        monkeypatch.setenv("MERIDIAN_TIMEOUT", "0.002")
    request = build_create_payload(
        SpawnCreateInput(
            prompt="wait forever",
            model="pi-test-model",
            harness=HarnessId.PI.value,
            project_root=tmp_path.as_posix(),
            timeout=0.001 if timeout_source == "cli" else None,
        ),
        runtime=build_runtime_from_root_and_config(tmp_path, load_config(tmp_path)),
    ).request
    assert request.execution_policy.timeout == 0.001
    assert request.launch_policy_snapshot is not None
    assert request.launch_policy_snapshot.execution_policy.timeout == 0.001

    run = Spawn(
        spawn_id=SpawnId("r-attempt-timeout-pi-abort"),
        prompt="wait forever",
        model=ModelId("pi-test-model"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-attempt-timeout-pi-abort",
        model=str(run.model),
        agent="",
        harness=HarnessId.PI.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=request,
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
        ),
        timeout=6.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code == 3
    assert row is not None
    assert row.status == "timed_out"
    assert row.terminal.exit_code == 3
    assert row.terminal.error == "timeout"
    history_path = runtime_root / "spawns" / str(run.spawn_id) / "history.jsonl"
    history = [json.loads(line) for line in history_path.read_text().splitlines()]
    finalized = [
        event
        for event in history
        if event["event_type"] == "meridian.pi.lifecycle.phase"
        and event["payload"].get("phase") == "finalized"
    ]
    assert finalized[-1]["payload"]["status"] == "timed_out"
    assert finalized[-1]["payload"]["exit_code"] == 3
    assert finalized[-1]["payload"]["error"] == "timeout"
    report = (runtime_root / "spawns" / str(run.spawn_id) / "report.md").read_text()
    assert report == "# Spawn failed\n\ntimeout\n"
    cleanup_phases = [
        event["payload"]["phase"]
        for event in history
        if event["event_type"] == "meridian.pi.lifecycle.phase"
        and str(event["payload"].get("phase", "")).startswith("cleanup_")
    ]
    assert cleanup_phases == ["cleanup_running", "cleanup_completed"]
    assert all(
        re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3,6}Z", event["timestamp"])
        for event in history
    )

    state = json.loads(
        (runtime_root / "spawns" / str(run.spawn_id) / "state.json").read_text()
    )
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3,6}Z",
        state["terminal"]["published_at"],
    )

@pytest.mark.asyncio
async def test_execute_with_streaming_finalizes_resident_deadline_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
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
    assert row.terminal.exit_code == 1
    assert row.terminal.error == "resident_deadline_expired"


@pytest.mark.asyncio
async def test_execute_with_streaming_keeps_resident_rearm_budget_across_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _ResidentRearmRetryConnection.reset(runtime_root)
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: (
            _ResidentRearmRetryConnection
        ),
    )

    run = Spawn(
        spawn_id=SpawnId("r-resident-rearm-retry"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-resident-rearm-retry",
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    request = _build_request().model_copy(
        update={
            "execution_policy": ResolvedExecutionPolicy(resident_rearm_budget=1),
            "retry": RetryPolicy(max_attempts=2, backoff_secs=0.0),
        }
    )
    guardrail = tmp_path / "retry-once.sh"
    marker = tmp_path / "guardrail-passed-once"
    guardrail.write_text(
        f'if [ ! -e "{marker}" ]; then touch "{marker}"; exit 1; fi\n',
        encoding="utf-8",
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
            guardrails=(guardrail,),
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code == 0
    assert _ResidentRearmRetryConnection.starts == 2
    assert row is not None
    assert row.status == "succeeded"
    assert row.resident_rearm_count == 1



@pytest.mark.asyncio
async def test_execute_with_streaming_does_not_retry_authoritative_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _ScriptedRetryOpenCodeConnection.reset(
        first_attempt_events=(
            RawHarnessEvent(
                event_type="session.error",
                harness_id="opencode",
                payload={
                    "type": "session.error",
                    "error": "connection reset by peer",
                    "sessionID": "session-retryable-opencode",
                },
            ),
        ),
        session_id="session-retryable-opencode",
        subprocess_pid=8383,
    )
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ScriptedRetryOpenCodeConnection,
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
    assert _ScriptedRetryOpenCodeConnection.starts == 1
    assert row is not None
    assert row.status == "failed"
    assert row.terminal.exit_code == 1
    assert row.terminal.error == "connection reset by peer"


@pytest.mark.asyncio
async def test_execute_with_streaming_retries_single_turn_close_without_terminal_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _ScriptedRetryOpenCodeConnection.reset(
        first_attempt_events=(),
        session_id="session-close-without-terminal-opencode",
        subprocess_pid=8484,
    )
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ScriptedRetryOpenCodeConnection,
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
    assert _ScriptedRetryOpenCodeConnection.starts == 2
    assert row is not None
    assert row.status == "succeeded"
    assert row.terminal.exit_code == 0
