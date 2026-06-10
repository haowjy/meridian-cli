# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Streaming runner setup-failure finalization behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import constants as launch_constants
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_runtime_root
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _build_request,
    _execute_with_context,
    _pi_extension_projection_fixture,
    _ReportThenHangConnection,
)
from tests.support.fakes import FakeClock, FakeHeartbeat

_pi_extension_projection_fixture = _pi_extension_projection_fixture


class _ControlSocketStartFails:
    def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: object) -> None:
        _ = spawn_id, socket_path, manager

    async def start(self) -> None:
        raise RuntimeError("Injected control socket setup failure")

    async def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_setup_failure_produces_terminal_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When setup raises, execute_with_streaming still writes a terminal event."""
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _ControlSocketStartFails)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ReportThenHangConnection,
    )

    run = Spawn(
        spawn_id=SpawnId("r-setup-fail"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-setup-fail",
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await _execute_with_context(
        run,
        request=_build_request(),
        project_root=tmp_path,
        runtime_root=runtime_root,
        artifacts=artifacts,
        registry=registry,
        clock=fake_clock,
        heartbeat_touch=fake_heartbeat.touch,
    )

    assert exit_code == launch_constants.DEFAULT_INFRA_EXIT_CODE
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "failed"
    assert row.exit_code == launch_constants.DEFAULT_INFRA_EXIT_CODE
    assert row.error is not None
    assert row.terminal_origin == "runner"
