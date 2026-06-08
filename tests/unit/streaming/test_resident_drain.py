from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.common import extract_codex_report
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
from meridian.lib.ops.spawn.outstanding import has_outstanding_descendant_work
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.unit.streaming.pi_quiescence_test_helpers import NoopControlServer


class _FakeLiveness:
    def __init__(self) -> None:
        self.awaiting_done_values: list[bool] = []

    def set_awaiting_done(self, awaiting_done: bool) -> None:
        self.awaiting_done_values.append(awaiting_done)


class _FakeResidentConnection(HarnessConnection[ResolvedLaunchSpec]):
    def __init__(self, harness_id: HarnessId) -> None:
        self._harness_id = harness_id
        self._spawn_id = SpawnId("")
        self._state: ConnectionState = "created"
        self._events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self._liveness = _FakeLiveness()
        self.stop_reasons: list[str | None] = []

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return self._harness_id

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def session_id(self) -> str | None:
        return "ses-resident"

    @property
    def subprocess_pid(self) -> int | None:
        return 4242

    @property
    def fake_liveness(self) -> _FakeLiveness:
        return self._liveness

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        self._state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = progress
        self.stop_reasons.append(reason)
        self._state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self) -> AsyncIterator[HarnessEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def emit(self, event: HarnessEvent) -> None:
        self._events.put_nowait(event)


def _event(harness_id: HarnessId, event_type: str, payload: dict[str, object]) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, harness_id=harness_id.value, payload=payload)


def _start_row(
    runtime_root: Path,
    spawn_id: str,
    harness: HarnessId,
    parent_id: str | None,
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=SpawnId(spawn_id),
        chat_id=spawn_id,
        parent_id=parent_id,
        model="test-model",
        agent="test-agent",
        harness=harness.value,
        prompt="hello",
        status="running",
    )


async def _start_manager(
    tmp_path: Path,
    connection: _FakeResidentConnection,
    *,
    spawn_id: SpawnId,
    resident_deadline_seconds: float = 30.0,
    resident_poll_seconds: float = 0.01,
) -> SpawnManager:
    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await connection.start(config, spec)
        return connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: NoopControlServer(),
    )
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=connection.harness_id,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            resident_deadline_seconds=resident_deadline_seconds,
            resident_poll_seconds=resident_poll_seconds,
        ),
        ResolvedLaunchSpec(
            harness=connection.harness_id.value,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )
    return manager


@pytest.mark.asyncio
async def test_codex_terminal_success_without_live_children_finalizes_immediately(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    _start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    connection = _FakeResidentConnection(HarnessId.CODEX)
    manager = await _start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(_event(HarnessId.CODEX, "turn/completed", {}))

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert True not in connection.fake_liveness.awaiting_done_values
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_opencode_terminal_success_without_live_children_finalizes_immediately(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    _start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    connection = _FakeResidentConnection(HarnessId.OPENCODE)
    manager = await _start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert True not in connection.fake_liveness.awaiting_done_values
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_opencode_terminal_success_resides_until_child_finishes(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p2")
    _start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    _start_row(tmp_path, str(child_id), HarnessId.CODEX, str(spawn_id))
    connection = _FakeResidentConnection(HarnessId.OPENCODE)
    manager = await _start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        assert connection.stop_reasons == []
        assert connection.fake_liveness.awaiting_done_values[-1] is True

        spawn_store.finalize_spawn(
            tmp_path,
            child_id,
            "succeeded",
            0,
            origin="runner",
        )
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert connection.fake_liveness.awaiting_done_values[-1] is False
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_codex_resident_deadline_finalizes_with_live_child(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    _start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    _start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    connection = _FakeResidentConnection(HarnessId.CODEX)
    manager = await _start_manager(
        tmp_path,
        connection,
        spawn_id=spawn_id,
        resident_deadline_seconds=0.02,
        resident_poll_seconds=0.01,
    )

    connection.emit(_event(HarnessId.CODEX, "turn/completed", {}))

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        child = spawn_store.get_spawn(tmp_path, "p2")
        assert child is not None
        assert child.status == "running"
    finally:
        await manager.stop_spawn(spawn_id)


def test_outstanding_descendant_work_is_pure_read(tmp_path: Path) -> None:
    _start_row(tmp_path, "p1", HarnessId.CODEX, None)
    _start_row(tmp_path, "p2", HarnessId.OPENCODE, "p1")

    before = spawn_store.get_spawn(tmp_path, "p2")
    assert has_outstanding_descendant_work(tmp_path, "p1") is True
    after = spawn_store.get_spawn(tmp_path, "p2")

    assert before == after
    assert after is not None
    assert after.status == "running"


@pytest.mark.asyncio
async def test_codex_resident_finalization_preserves_artifact_report(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p2")
    _start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    _start_row(tmp_path, str(child_id), HarnessId.OPENCODE, str(spawn_id))
    connection = _FakeResidentConnection(HarnessId.CODEX)
    manager = await _start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(
        _event(
            HarnessId.CODEX,
            "item/completed",
            {"item": {"type": "agentMessage", "text": "Resident report."}},
        )
    )
    connection.emit(_event(HarnessId.CODEX, "turn/completed", {}))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        spawn_store.finalize_spawn(
            tmp_path,
            child_id,
            "succeeded",
            0,
            origin="runner",
        )
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert extract_codex_report(LocalStore(root_dir=tmp_path / "spawns"), spawn_id) == (
            "Resident report."
        )
    finally:
        await manager.stop_spawn(spawn_id)
