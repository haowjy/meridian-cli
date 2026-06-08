from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.common import extract_codex_report
from meridian.lib.harness.connections import liveness as liveness_module
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.connections.liveness import BackendLivenessPolicy, LivenessDecision
from meridian.lib.harness.connections.resident_backend import (
    LivenessResidentBackendControl,
    ResidentBackendControl,
)
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.ops.spawn.models import SpawnSignalInput
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.spawn_tree import has_outstanding_descendant_work
from meridian.lib.streaming.drain_policy import (
    TURN_BOUNDARY_EVENT_TYPE,
    DrainPolicy,
    PersistentDrainPolicy,
)
from meridian.lib.streaming.resident_drain import ResidentDrainCoordinator
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.fakes import FakeClock
from tests.unit.streaming.pi_quiescence_test_helpers import NoopControlServer


class _FakeResidentBackendControl:
    def __init__(self) -> None:
        self.awaiting_done_values: list[bool] = []
        self.status: LivenessDecision = LivenessDecision.CONTINUE
        self.injected_messages: list[str] = []
        self.fail_inject: bool = False

    def health_status(self) -> LivenessDecision:
        return self.status

    def set_awaiting_done(self, awaiting: bool) -> None:
        self.awaiting_done_values.append(awaiting)

    async def begin_followup_turn(self, message: str) -> None:
        if self.fail_inject:
            raise RuntimeError("inject failed")
        self.injected_messages.append(message)


class _FakeResidentConnection(HarnessConnection[ResolvedLaunchSpec]):
    def __init__(self, harness_id: HarnessId) -> None:
        self._harness_id = harness_id
        self._spawn_id = SpawnId("")
        self._state: ConnectionState = "created"
        self._events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self._resident_backend = _FakeResidentBackendControl()
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
    def fake_resident_backend(self) -> _FakeResidentBackendControl:
        return self._resident_backend

    @property
    def resident_backend(self) -> ResidentBackendControl:
        return self._resident_backend

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
        self._resident_backend.status = LivenessDecision.BACKEND_DEAD

    def mark_stalled(self) -> None:
        self._resident_backend.status = LivenessDecision.STREAM_STALLED


class _LivenessBackedResidentConnection(_FakeResidentConnection):
    def __init__(
        self,
        harness_id: HarnessId,
        *,
        liveness: BackendLivenessPolicy,
        backend_dead: bool = False,
    ) -> None:
        super().__init__(harness_id)
        self._backend_dead = backend_dead
        self._liveness_resident_backend = LivenessResidentBackendControl(
            liveness=liveness,
            backend_dead=lambda: self._backend_dead,
            begin_followup_turn=self._noop_followup_turn,
        )

    @property
    def resident_backend(self) -> ResidentBackendControl:
        return self._liveness_resident_backend

    async def _noop_followup_turn(self, message: str) -> None:
        _ = message


def _silent_liveness_policy(
    clock: FakeClock,
    *,
    pid: int | None = 4242,
) -> BackendLivenessPolicy:
    policy = BackendLivenessPolicy(
        timeout_seconds=lambda: 10.0,
        now=clock.monotonic,
        backend_pid=lambda: pid,
        backend_birth_time=lambda: None,
    )
    policy.mark_activity()
    clock.advance(11.0)
    return policy


def _awaiting_done_coordinator(
    tmp_path: Path,
    connection: HarnessConnection[Any],
) -> ResidentDrainCoordinator:
    coordinator = ResidentDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        receiver=connection,
        deadline_seconds=30.0,
        poll_seconds=0.01,
    )
    coordinator.pending_outcome = TerminalEventOutcome(status="succeeded", exit_code=0)
    coordinator.deadline_monotonic = time.monotonic() + coordinator.deadline_seconds
    return coordinator


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
    project_root: Path | None = None,
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
        project_root=project_root or tmp_path,
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
        assert True not in connection.fake_resident_backend.awaiting_done_values
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
        assert True not in connection.fake_resident_backend.awaiting_done_values
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
        assert True not in connection.fake_resident_backend.awaiting_done_values
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
        assert connection.fake_resident_backend.awaiting_done_values[-1] is True

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
        assert connection.fake_resident_backend.awaiting_done_values[-1] is False
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_done_signal_at_terminal_event_wins_over_outstanding_child(
    tmp_path: Path,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal

    spawn_id = SpawnId("p1")
    _start_row(tmp_path, str(spawn_id), HarnessId.OPENCODE, None)
    _start_row(tmp_path, "p2", HarnessId.CODEX, str(spawn_id))
    write_spawn_signal(tmp_path, spawn_id, "done")
    write_spawn_signal(tmp_path, spawn_id, "rearm")
    connection = _FakeResidentConnection(HarnessId.OPENCODE)
    manager = await _start_manager(tmp_path, connection, spawn_id=spawn_id)

    connection.emit(_event(HarnessId.OPENCODE, "session.idle", {}))

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert True not in connection.fake_resident_backend.awaiting_done_values
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_done_op_releases_resident_wait_via_environment_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    runtime_root = prepared.runtime_root
    assert runtime_root is not None
    spawn_id = SpawnId("p1")
    _start_row(runtime_root, str(spawn_id), HarnessId.OPENCODE, None)
    _start_row(runtime_root, "p2", HarnessId.CODEX, str(spawn_id))
    connection = _FakeResidentConnection(HarnessId.OPENCODE)
    manager = await _start_manager(
        runtime_root,
        connection,
        spawn_id=spawn_id,
        project_root=project_root,
    )

    connection.emit(_event(HarnessId.OPENCODE, "session.idle", {}))
    completion_task = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await asyncio.sleep(0.03)
        assert not completion_task.done()
        monkeypatch.setenv("MERIDIAN_SPAWN_ID", str(spawn_id))

        result = spawn_api.spawn_done_sync(SpawnSignalInput(), prepared=prepared)
        outcome = await asyncio.wait_for(completion_task, timeout=0.5)

        assert result.status == "succeeded"
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert connection.fake_resident_backend.awaiting_done_values[-1] is False
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_rearm_op_extends_resident_deadline(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    runtime_root = prepared.runtime_root
    assert runtime_root is not None
    spawn_id = SpawnId("p1")
    _start_row(runtime_root, str(spawn_id), HarnessId.CODEX, None)
    _start_row(runtime_root, "p2", HarnessId.CODEX, str(spawn_id))
    connection = _FakeResidentConnection(HarnessId.CODEX)
    manager = await _start_manager(
        runtime_root,
        connection,
        spawn_id=spawn_id,
        project_root=project_root,
        resident_deadline_seconds=0.2,
        resident_poll_seconds=0.01,
    )

    connection.emit(_event(HarnessId.CODEX, "turn/completed", {}))
    completion_task = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await asyncio.sleep(0.1)
        assert not completion_task.done()

        result = spawn_api.spawn_rearm_sync(
            SpawnSignalInput(spawn_id=str(spawn_id)),
            ctx=RuntimeContext(spawn_id=spawn_id),
            prepared=prepared,
        )
        await asyncio.sleep(0.12)

        assert result.status == "succeeded"
        assert not completion_task.done()

        done_result = spawn_api.spawn_done_sync(
            SpawnSignalInput(spawn_id=str(spawn_id)),
            prepared=prepared,
        )
        outcome = await asyncio.wait_for(completion_task, timeout=0.5)
        assert done_result.status == "succeeded"
        assert outcome is not None
        assert outcome.status == "succeeded"
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
async def test_resident_stream_close_with_stalled_backend_is_not_dead_outcome(
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
        connection.mark_stalled()
        connection.close_stream()

        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=0.5)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "stream_closed_while_awaiting_done"
    finally:
        await manager.stop_spawn(spawn_id)


def test_resident_close_classifies_dead_backend_through_liveness_policy(
    tmp_path: Path,
) -> None:
    clock = FakeClock(start=0.0)
    connection = _LivenessBackedResidentConnection(
        HarnessId.OPENCODE,
        liveness=_silent_liveness_policy(clock, pid=None),
    )
    coordinator = _awaiting_done_coordinator(tmp_path, connection)

    assert connection.resident_backend.health_status() == LivenessDecision.BACKEND_DEAD

    outcome = coordinator.handle_close(intentional_stop=False)

    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error == "backend_dead_while_awaiting_done"


def test_resident_close_preserves_stalled_stream_through_liveness_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(liveness_module, "is_process_alive", lambda *_args, **_kwargs: True)
    clock = FakeClock(start=0.0)
    connection = _LivenessBackedResidentConnection(
        HarnessId.OPENCODE,
        liveness=_silent_liveness_policy(clock),
    )
    coordinator = _awaiting_done_coordinator(tmp_path, connection)

    assert connection.resident_backend.health_status() == LivenessDecision.STREAM_STALLED
    connection.resident_backend.set_awaiting_done(True)
    assert connection.resident_backend.health_status() == LivenessDecision.STREAM_STALLED

    outcome = coordinator.handle_close(intentional_stop=False)

    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error == "stream_closed_while_awaiting_done"


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
        assert connection.fake_resident_backend.awaiting_done_values[-1] is True

        outcome = await asyncio.wait_for(completion_task, timeout=0.5)
        assert outcome is not None
        assert outcome.status == "timed_out"
        assert outcome.error == "resident_deadline_expired"
        assert reaped_spawn_ids == ["p2"]
        assert connection.fake_resident_backend.awaiting_done_values[-1] is False
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


def _coordinator_with_clock(
    tmp_path: Path,
    connection: _FakeResidentConnection,
    clock: FakeClock,
    *,
    deadline_seconds: float = 3300.0,
    poll_seconds: float = 5.0,
) -> ResidentDrainCoordinator:
    coordinator = ResidentDrainCoordinator.for_connection(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        receiver=connection,
        deadline_seconds=deadline_seconds,
        poll_seconds=poll_seconds,
    )
    coordinator.pending_outcome = TerminalEventOutcome(status="succeeded", exit_code=0)
    coordinator.deadline_monotonic = clock.monotonic() + deadline_seconds
    coordinator._set_awaiting_done(True)
    return coordinator


@pytest.mark.asyncio
async def test_rearm_signal_extends_deadline_and_injects_only_after_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=100.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    _start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = _FakeResidentConnection(HarnessId.CODEX)
    coordinator = _coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=3300.0)

    clock.advance(10.0)
    write_spawn_signal(tmp_path, "p1", "rearm")
    old_deadline = coordinator.deadline_monotonic
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert coordinator.resident_requested is True
    assert old_deadline is not None
    assert coordinator.deadline_monotonic is not None
    assert coordinator.deadline_monotonic > old_deadline
    assert connection.fake_resident_backend.injected_messages == []
    assert coordinator.next_timeout() == pytest.approx(5.0)

    clock.advance(270.0)
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert len(connection.fake_resident_backend.injected_messages) == 1
    injected = connection.fake_resident_backend.injected_messages[0]
    assert "meridian spawn done" in injected
    assert "meridian spawn rearm" in injected

    write_spawn_signal(tmp_path, "p1", "done")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "succeeded"
    assert coordinator.pending_outcome is None


@pytest.mark.asyncio
async def test_active_followup_turn_stays_resident_honors_done_and_defers_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=100.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    _start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = _FakeResidentConnection(HarnessId.CODEX)
    coordinator = _coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=300.0)
    coordinator.resident_requested = True
    coordinator.next_inject_monotonic = clock.monotonic()

    coordinator.observe_activity_transition("turn_active")

    assert coordinator.turn_active is True
    assert coordinator.pending_outcome is not None
    assert coordinator.deadline_monotonic is not None
    assert coordinator.deadline_monotonic > clock.monotonic()
    assert coordinator.next_inject_monotonic is not None
    assert coordinator.next_inject_monotonic > clock.monotonic()
    assert coordinator.next_timeout() == pytest.approx(5.0)

    write_spawn_signal(tmp_path, "p1", "done")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "succeeded"
    assert coordinator.deadline_monotonic is None


@pytest.mark.asyncio
async def test_active_followup_turn_still_enforces_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=0.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    _start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = _FakeResidentConnection(HarnessId.CODEX)
    coordinator = _coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=10.0)
    coordinator.observe_activity_transition("turn_active")

    clock.advance(10.0)
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "timed_out"
    assert decision.recorded_outcome.error == "resident_deadline_expired"


@pytest.mark.asyncio
async def test_deadline_returns_timed_out_when_descendant_reap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=0.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    _start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = _FakeResidentConnection(HarnessId.CODEX)
    coordinator = _coordinator_with_clock(tmp_path, connection, clock, deadline_seconds=10.0)

    def _fail_reap() -> None:
        raise RuntimeError("teardown failed")

    monkeypatch.setattr(coordinator, "terminate_outstanding_descendants", _fail_reap)
    clock.advance(10.0)

    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "timed_out"
    assert decision.recorded_outcome.error == "resident_deadline_expired"


@pytest.mark.asyncio
async def test_rearmed_with_tracked_child_uses_poll_timeout_not_inject_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=0.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    _start_row(tmp_path, "p1", HarnessId.CODEX, None)
    _start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    connection = _FakeResidentConnection(HarnessId.CODEX)
    coordinator = _coordinator_with_clock(tmp_path, connection, clock)
    coordinator.resident_requested = True
    coordinator.next_inject_monotonic = clock.monotonic()

    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert connection.fake_resident_backend.injected_messages == []
    assert coordinator.next_timeout() == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_failed_inject_advances_cadence_and_uses_poll_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.streaming import resident_drain as resident_drain_module

    clock = FakeClock(start=0.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    _start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = _FakeResidentConnection(HarnessId.CODEX)
    connection.fake_resident_backend.fail_inject = True
    coordinator = _coordinator_with_clock(tmp_path, connection, clock)
    coordinator.resident_requested = True
    coordinator.next_inject_monotonic = clock.monotonic()

    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert connection.fake_resident_backend.injected_messages == []
    assert coordinator.next_inject_monotonic is not None
    assert coordinator.next_inject_monotonic > clock.monotonic()
    assert coordinator.next_timeout() == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_done_signal_is_honored_with_tracked_child_outstanding(
    tmp_path: Path,
) -> None:
    from meridian.lib.state.spawn_signals import write_spawn_signal

    _start_row(tmp_path, "p1", HarnessId.OPENCODE, None)
    _start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    connection = _FakeResidentConnection(HarnessId.OPENCODE)
    coordinator = _awaiting_done_coordinator(tmp_path, connection)

    write_spawn_signal(tmp_path, "p1", "done")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "succeeded"
    assert connection.fake_resident_backend.injected_messages == []


def test_spawn_signal_write_consume_round_trip(tmp_path: Path) -> None:
    from meridian.lib.state.spawn_signals import (
        consume_spawn_signal,
        spawn_signal_path,
        write_spawn_signal,
    )

    write_spawn_signal(tmp_path, "p1", "done")
    write_spawn_signal(tmp_path, "p1", "rearm")

    assert spawn_signal_path(tmp_path, "p1", "done").is_file()
    assert consume_spawn_signal(tmp_path, "p1", "done") is True
    assert consume_spawn_signal(tmp_path, "p1", "done") is False
    assert consume_spawn_signal(tmp_path, "p1", "rearm") is True
