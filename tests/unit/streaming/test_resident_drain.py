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
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.spawn_tree import has_outstanding_descendant_work
from meridian.lib.streaming.drain_policy import (
    TURN_BOUNDARY_EVENT_TYPE,
    DrainPolicy,
    PersistentDrainPolicy,
)
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

    def close_stream(self) -> None:
        self._events.put_nowait(None)

    def mark_failed(self) -> None:
        self._state = "failed"


def _event(harness_id: HarnessId, event_type: str, payload: dict[str, object]) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, harness_id=harness_id.value, payload=payload)


async def _next_turn_boundary(
    subscriber: asyncio.Queue[HarnessEvent | None],
) -> HarnessEvent:
    while True:
        event = await asyncio.wait_for(subscriber.get(), timeout=0.5)
        assert event is not None
        if event.event_type == TURN_BOUNDARY_EVENT_TYPE:
            return event


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
    drain_policy: DrainPolicy | None = None,
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
        drain_policy=drain_policy,
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


@pytest.mark.parametrize(
    "harness_id,event_type",
    [
        (HarnessId.CODEX, "turn/completed"),
        (HarnessId.OPENCODE, "session.idle"),
    ],
)
@pytest.mark.asyncio
async def test_resident_persistent_policy_emits_boundary_and_stays_alive(
    tmp_path: Path,
    harness_id: HarnessId,
    event_type: str,
) -> None:
    spawn_id = SpawnId("p1")
    _start_row(tmp_path, str(spawn_id), harness_id, None)
    connection = _FakeResidentConnection(harness_id)
    manager = await _start_manager(
        tmp_path,
        connection,
        spawn_id=spawn_id,
        drain_policy=PersistentDrainPolicy(),
    )
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    connection.emit(_event(harness_id, event_type, {}))

    try:
        await _next_turn_boundary(subscriber)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        assert manager.get_connection(spawn_id) is connection
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
        assert manager.get_connection(spawn_id) is connection
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
async def test_resident_wait_fans_out_turn_boundary_to_subscriber(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    _start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    _start_row(tmp_path, "p2", HarnessId.OPENCODE, str(spawn_id))
    connection = _FakeResidentConnection(HarnessId.CODEX)
    manager = await _start_manager(tmp_path, connection, spawn_id=spawn_id)
    subscriber = manager.subscribe(spawn_id)
    assert subscriber is not None

    connection.emit(_event(HarnessId.CODEX, "turn/completed", {}))

    try:
        boundary = await _next_turn_boundary(subscriber)
        assert boundary.payload["status"] == "succeeded"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_child_written_before_terminal_event_is_processed_prevents_early_finalize(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    child_id = SpawnId("p2")
    _start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    connection = _FakeResidentConnection(HarnessId.CODEX)
    manager = await _start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(_event(HarnessId.CODEX, "turn/completed", {}))
    _start_row(tmp_path, str(child_id), HarnessId.OPENCODE, str(spawn_id))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        spawn_store.finalize_spawn(tmp_path, child_id, "succeeded", 0, origin="runner")
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_resident_stream_close_with_dead_backend_fails_while_child_running(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    _start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    _start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    connection = _FakeResidentConnection(HarnessId.OPENCODE)
    manager = await _start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        connection.mark_failed()
        connection.close_stream()

        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "backend_dead_while_awaiting_done"
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_codex_resident_deadline_waits_then_reaps_live_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("p1")
    _start_row(tmp_path, str(spawn_id), HarnessId.CODEX, None)
    _start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    reaped_spawn_ids: list[str] = []

    def _record_teardown(runtime_root: Path, spawn_record: Any, **kwargs: object) -> list[object]:
        _ = runtime_root, kwargs
        reaped_spawn_ids.append(spawn_record.id)
        return []

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_spawn_scopes",
        _record_teardown,
    )
    connection = _FakeResidentConnection(HarnessId.CODEX)
    manager = await _start_manager(
        tmp_path,
        connection,
        spawn_id=spawn_id,
        resident_deadline_seconds=0.08,
        resident_poll_seconds=0.01,
    )

    connection.emit(_event(HarnessId.CODEX, "turn/completed", {}))
    completion_task = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await asyncio.sleep(0.03)
        assert not completion_task.done()
        assert connection.fake_liveness.awaiting_done_values[-1] is True

        outcome = await asyncio.wait_for(completion_task, timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert reaped_spawn_ids == ["p2"]
        assert connection.fake_liveness.awaiting_done_values[-1] is False
    finally:
        await manager.stop_spawn(spawn_id)


def test_outstanding_descendant_work_is_pure_read_for_grandchild(tmp_path: Path) -> None:
    _start_row(tmp_path, "p1", HarnessId.CODEX, None)
    _start_row(tmp_path, "p2", HarnessId.OPENCODE, "p1")
    _start_row(tmp_path, "p3", HarnessId.CODEX, "p2")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p2"), "succeeded", 0, origin="runner")

    before = spawn_store.list_spawns(tmp_path)
    assert has_outstanding_descendant_work("p1", spawn_store.list_spawns(tmp_path)) is True
    after = spawn_store.list_spawns(tmp_path)

    assert before == after
    grandchild = spawn_store.get_spawn(tmp_path, "p3")
    assert grandchild is not None
    assert grandchild.status == "running"


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
