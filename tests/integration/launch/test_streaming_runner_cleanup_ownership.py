# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Streaming runner cleanup ownership behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.platform.process_scope import fallback as process_scope_fallback
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import InMemoryStore, LocalStore, make_artifact_key
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _build_opencode_request,
    _build_request,
    _CodexTerminalWithScopeConnection,
    _execute_with_context,
    _FakeControlSocketServer,
    _OpenCodeTerminalWithScopeConnection,
    _pi_extension_projection_fixture,
)
from tests.support.fakes import FakeClock, FakeHeartbeat

_pi_extension_projection_fixture = _pi_extension_projection_fixture


def test_persist_attempt_artifacts_does_not_mirror_history(tmp_path: Path) -> None:
    from meridian.lib.launch import streaming_runner

    spawn_id = SpawnId("p-single-history")
    (tmp_path / "history.jsonl").write_bytes(b'{"event_type":"turn/completed"}\n')
    (tmp_path / "stderr.log").write_bytes(b"diagnostic\n")
    artifacts = InMemoryStore()

    streaming_runner._persist_attempt_artifacts(
        artifacts=artifacts,
        spawn_id=spawn_id,
        log_dir=tmp_path,
    )

    assert not artifacts.exists(make_artifact_key(spawn_id, "history.jsonl"))
    assert artifacts.get(make_artifact_key(spawn_id, "stderr.log")) == b"diagnostic\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness_id", "connection_cls", "spawn_request"),
    (
        (HarnessId.OPENCODE, _OpenCodeTerminalWithScopeConnection, _build_opencode_request()),
        (HarnessId.CODEX, _CodexTerminalWithScopeConnection, _build_request()),
    ),
)
async def test_execute_with_streaming_routes_backend_cleanup_through_stop_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness_id: HarnessId,
    connection_cls: type[_OpenCodeTerminalWithScopeConnection],
    spawn_request: SpawnRequest,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    cleanup_calls: list[dict[str, object]] = []

    def _fake_terminate_tree_sync(
        *,
        pid: int,
        created_at_epoch: float,
        grace_secs: float,
        reason: str,
        scope_id: str,
    ) -> object:
        cleanup_calls.append(
            {
                "pid": pid,
                "created_at_epoch": created_at_epoch,
                "grace_secs": grace_secs,
                "reason": reason,
                "scope_id": scope_id,
            }
        )
        return object()

    monkeypatch.setattr(process_scope_fallback, "terminate_tree_sync", _fake_terminate_tree_sync)
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    def _get_connection_class(
        _harness_id: HarnessId,
        _transport_id: TransportId = TransportId.STREAMING,
    ) -> type[_OpenCodeTerminalWithScopeConnection]:
        return connection_cls

    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        _get_connection_class,
    )

    run = Spawn(
        spawn_id=SpawnId(f"r-{harness_id.value}-cleanup"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex" if harness_id is HarnessId.CODEX else "gpt-5.4"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id=f"test-chat-{harness_id.value}-cleanup",
        model=str(run.model),
        agent="",
        harness=harness_id.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=spawn_request,
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

    assert exit_code == 0
    assert cleanup_calls == [
        {
            "pid": 73737,
            "created_at_epoch": 12_345.0,
            "grace_secs": 5.0,
            "reason": "stop_called",
            "scope_id": "backend",
        }
    ]
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "succeeded"
