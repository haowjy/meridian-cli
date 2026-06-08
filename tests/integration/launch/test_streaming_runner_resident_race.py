"""Regression coverage for resident drain vs runner terminal arbitration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.streaming_runner import _run_streaming_attempt
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.spawn.model import BACKGROUND_LAUNCH_MODE
from meridian.lib.state.spawn_store import finalize_spawn, start_spawn
from meridian.lib.streaming.spawn_manager import SpawnManager


class _FakeControlSocketServer:
    endpoint: str | None = None

    def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: SpawnManager) -> None:
        _ = spawn_id, socket_path, manager

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _ResidentCodexConnection(HarnessConnection[ResolvedLaunchSpec]):
    def __init__(self) -> None:
        self._spawn_id = SpawnId("")
        self.release_terminal = asyncio.Event()
        self.terminal_yielded = asyncio.Event()
        self.stopped = False
        self.cancelled = False

    @property
    def state(self) -> ConnectionState:
        return "stopped" if self.stopped else "connected"

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=False,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=False,
        )

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def subprocess_pid(self) -> int | None:
        return None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        self.stopped = True
        return StopResult()

    def health(self) -> bool:
        return True

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        self.cancelled = True

    async def events(self) -> AsyncIterator[HarnessEvent]:
        await self.release_terminal.wait()
        yield HarnessEvent(event_type="turn/completed", payload={}, harness_id="codex")
        self.terminal_yielded.set()
        while not self.stopped:
            await asyncio.sleep(0.01)


def _connection_config(spawn_id: SpawnId, project_root: Path) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=HarnessId.CODEX,
        prompt="parent",
        control_root=project_root,
        env_overrides={},
        resident_deadline_seconds=5.0,
        resident_poll_seconds=0.01,
    )


def _launch_spec() -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        prompt="parent",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


@pytest.mark.asyncio
async def test_resident_runner_waits_for_coordinator_after_raw_terminal_frame(
    tmp_path: Path,
) -> None:
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir
    parent_id = SpawnId("p-parent")
    child_id = SpawnId("p-child")
    start_spawn(
        runtime_root,
        spawn_id=parent_id,
        chat_id="chat",
        model="codex-test",
        agent="agent",
        harness=HarnessId.CODEX.value,
        prompt="parent",
        status="running",
    )
    start_spawn(
        runtime_root,
        spawn_id=child_id,
        parent_id=str(parent_id),
        chat_id="chat",
        model="codex-test",
        agent="agent",
        harness=HarnessId.CODEX.value,
        prompt="child",
        status="running",
    )

    connection = _ResidentCodexConnection()

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await connection.start(config, spec)
        return connection

    manager = SpawnManager(
        runtime_root=runtime_root,
        project_root=project_root,
        start_connection=_start_connection,
        control_server_factory=cast("Any", _FakeControlSocketServer),
    )
    run = Spawn(
        spawn_id=parent_id,
        prompt="parent",
        model=ModelId("codex-test"),
        status="running",
    )
    signal_event = asyncio.Event()
    received_signal: list[Any] = [None]

    attempt_task = asyncio.create_task(
        _run_streaming_attempt(
            run=run,
            runtime_root=runtime_root,
            launch_mode=BACKGROUND_LAUNCH_MODE,
            log_dir=tmp_path,
            manager=manager,
            config=_connection_config(parent_id, project_root),
            run_spec=_launch_spec(),
            budget_tracker=None,
            signal_event=signal_event,
            received_signal=received_signal,
            timeout_seconds=None,
            event_observer=None,
            stream_stdout_to_terminal=False,
            lifecycle_service=SpawnLifecycleService(runtime_root),
        )
    )

    while parent_id not in manager._sessions or manager._sessions[parent_id].subscriber is None:
        await asyncio.sleep(0)

    connection.release_terminal.set()
    await asyncio.wait_for(connection.terminal_yielded.wait(), timeout=1.0)
    await asyncio.sleep(0.05)

    assert not attempt_task.done()
    assert connection.stopped is False

    finalize_spawn(
        runtime_root,
        child_id,
        "succeeded",
        0,
        origin="runner",
        duration_secs=0.1,
    )

    attempt = await asyncio.wait_for(attempt_task, timeout=1.0)
    assert attempt.drain_exit_code == 0
    assert attempt.drain_error is None
    assert attempt.cancelled_by_request is False
