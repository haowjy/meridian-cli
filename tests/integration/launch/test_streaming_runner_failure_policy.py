# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Streaming runner terminal failure and resident deadline behavior."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.domain import Spawn
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.ops.runtime import build_runtime_from_root_and_config
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.streaming import pi_drain as pi_drain_module
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _build_request,
    _execute_with_context,
    _FakeControlSocketServer,
    _pi_extension_projection_fixture,
    _ResidentDeadlineConnection,
    _ResidentGuardrailConnection,
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
    async def _abort_tail_exit_failure(_coordinator: object, _recorded_outcome: object) -> object:
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
    report = (runtime_root / "spawns" / str(run.spawn_id) / "report.md").read_text()
    assert report == "# Spawn failed\n\ntimeout\n"
    lifecycle = json.loads(
        (runtime_root / "spawns" / str(run.spawn_id) / "pi-lifecycle.json").read_text()
    )
    assert lifecycle["phase"] == "cleanup_completed"
    assert lifecycle["cleanup_status"] == "completed"

    state = json.loads((runtime_root / "spawns" / str(run.spawn_id) / "state.json").read_text())
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3,6}Z",
        state["terminal"]["published_at"],
    )


@pytest.mark.asyncio
async def test_execute_with_streaming_finalizes_resident_deadline_after_one_start(
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
    request = _build_request()
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


@pytest.mark.parametrize(
    ("terminal_script", "expected_error"),
    [
        (
            "printf '%s\n' "
            "'{\"type\":\"result\",\"subtype\":\"error_max_turns\",\"is_error\":true,"
            "\"result\":\"subscription quota exhausted\"}'\n",
            "subscription quota exhausted",
        ),
        ('printf "%s\n" "connection reset by peer" >&2\n', None),
    ],
    ids=("explicit-quota-result", "post-init-transport-failure"),
)
@pytest.mark.asyncio
async def test_claude_failure_runs_one_real_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_script: str,
    expected_error: str | None,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "claude-invocations"
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n'
        f'printf "%s\n" "$*" >> {invocation_log}\n'
        "session_id=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        "IFS= read -r prompt\n"
        "printf '{\"type\":\"system\",\"subtype\":\"init\","
        "\"session_id\":\"%s\"}\n' \"$session_id\"\n"
        f"{terminal_script}"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    run = Spawn(
        spawn_id=SpawnId("r-claude-single-attempt"),
        prompt="do work",
        model=ModelId("claude-sonnet-4-5"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-claude-single-attempt",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=SpawnRequest(
                model=str(run.model),
                harness=HarnessId.CLAUDE.value,
                prompt=run.prompt,
            ),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=LocalStore(root_dir=tmp_path / ".artifacts"),
            registry=HarnessRegistry.with_defaults(),
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code == 1
    assert row is not None and row.terminal is not None
    if expected_error is not None:
        assert row.terminal.error == expected_error
    else:
        assert row.terminal.error
    assert invocation_log.read_text(encoding="utf-8").count("\n") == 1

    spawn_dir = runtime_root / "spawns" / str(run.spawn_id)
    assert not any(path.name.startswith("attempt-2") for path in spawn_dir.iterdir())
    lifecycle_rows = [
        json.loads(line)
        for line in (spawn_dir / "runner-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["attempt"] for row in lifecycle_rows if row["event"] == "attempt_started"] == [
        1
    ]


@pytest.mark.asyncio
async def test_guardrail_failure_does_not_rerun_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    _ResidentGuardrailConnection.reset(runtime_root)
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ResidentGuardrailConnection,
    )

    run = Spawn(
        spawn_id=SpawnId("r-guardrail-single-attempt"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-guardrail-single-attempt",
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    guardrail = tmp_path / "fail.sh"
    guardrail.write_text("exit 1\n", encoding="utf-8")

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=_build_request().model_copy(
                update={
                    "execution_policy": ResolvedExecutionPolicy(resident_rearm_budget=1),
                }
            ),
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
    assert exit_code == 1
    assert _ResidentGuardrailConnection.starts == 1
    assert row is not None and row.terminal is not None
    assert row.status == "failed"
    assert row.terminal.error == "guardrail_failed"
