# qa-validated: pi-rpc-quiescence
"""Pi RPC quiescence policy and tracker tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessConnection,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.streaming.drain_policy import DrainAction, PiRpcQuiescenceDrainPolicy
from meridian.lib.streaming.spawn_manager import SpawnManager, _PiSubspawnTracker


class _NoopControlServer:
    endpoint = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _FakePiConnection(HarnessConnection[ResolvedLaunchSpec]):
    def __init__(self, events: list[HarnessEvent]) -> None:
        self._events = events
        self._spawn_id = SpawnId("")
        self._state = "created"
        self.stop_reasons: list[str | None] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.PI

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
        return "ses-pi"

    @property
    def subprocess_pid(self) -> int | None:
        return 4242

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

    async def events(self):  # type: ignore[no-untyped-def]
        for event in self._events:
            yield event


def _pi_event(event_type: str, payload: dict[str, object]) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, harness_id="pi", payload=payload)


def test_pi_quiescence_policy_waits_for_callback_state() -> None:
    quiescent = False

    def _check() -> bool:
        return quiescent

    policy = PiRpcQuiescenceDrainPolicy(quiescence_check=_check)

    assert policy.classify(TerminalEventOutcome(status="succeeded", exit_code=0)) == DrainAction(
        terminate=False,
        emit_turn_boundary=True,
    )

    quiescent = True
    assert policy.classify(TerminalEventOutcome(status="succeeded", exit_code=0)) == DrainAction(
        terminate=True,
        emit_turn_boundary=False,
    )
    assert policy.classify(TerminalEventOutcome(status="failed", exit_code=1)) == DrainAction(
        terminate=True,
        emit_turn_boundary=False,
    )


def test_pi_subspawn_tracker_ignores_detached_wait_policy_and_tracks_notifications() -> None:
    tracker = _PiSubspawnTracker.empty()

    tracker.observe(
        _pi_event(
            "meridian.subspawn.start",
            {"subspawn_id": "detached-1", "wait_policy": "detached"},
        )
    )
    assert tracker.has_pending() is False

    tracker.observe(
        _pi_event(
            "meridian.subspawn.start",
            {
                "subspawn_id": "tracked-1",
                "wait_policy": "tracked",
                "pid": 4401,
            },
        )
    )
    assert tracker.has_pending() is True
    assert tracker.active_tracked_pgid_candidates() == (4401,)

    tracker.observe(
        _pi_event(
            "meridian.notification.queued",
            {"notification_id": "n-1"},
        )
    )
    assert tracker.has_pending_notifications() is True

    tracker.observe(
        _pi_event(
            "meridian.notification.completed",
            {"notification_id": "n-1"},
        )
    )
    assert tracker.has_pending_notifications() is False

    tracker.observe(
        _pi_event(
            "meridian.notification.failed",
            {"notification_id": "n-2", "reason": "sendMessage_error"},
        )
    )
    assert tracker.notification_failure_error == "pi_notification_failed:sendMessage_error"

    tracker.observe(
        _pi_event(
            "meridian.subspawn.end",
            {"subspawn_id": "tracked-1", "wait_policy": "tracked"},
        )
    )
    assert tracker.has_pending() is False
    assert tracker.active_tracked_pgid_candidates() == ()


def test_pi_subspawn_tracker_invalidates_missing_canonical_ids() -> None:
    tracker = _PiSubspawnTracker.empty()

    tracker.observe(
        _pi_event(
            "meridian.subspawn.start",
            {"wait_policy": "tracked"},
        )
    )
    assert tracker.has_pending() is False
    assert (
        tracker.lifecycle_tracking_invalidated_error
        == "pi_lifecycle_tracking_invalidated:missing_subspawn_id:meridian.subspawn.start"
    )

    notification_tracker = _PiSubspawnTracker.empty()
    notification_tracker.observe(
        _pi_event(
            "meridian.notification.queued",
            {},
        )
    )
    assert notification_tracker.has_pending_notifications() is False
    assert (
        notification_tracker.lifecycle_tracking_invalidated_error
        == "pi_lifecycle_tracking_invalidated:missing_notification_id:meridian.notification.queued"
    )


def test_pi_subspawn_tracker_tracks_meridian_spawn_kind_by_spawn_id() -> None:
    tracker = _PiSubspawnTracker.empty()

    tracker.observe(
        _pi_event(
            "meridian.subspawn.start",
            {
                "schema_version": 1,
                "subspawn_id": "p123",
                "kind": "meridian_spawn",
                "wait_policy": "tracked",
            },
        )
    )
    assert tracker.has_pending() is True

    tracker.observe(
        _pi_event(
            "meridian.subspawn.end",
            {
                "schema_version": 1,
                "subspawn_id": "p123",
                "kind": "meridian_spawn",
                "wait_policy": "tracked",
            },
        )
    )
    assert tracker.has_pending() is False


def test_pi_subspawn_tracker_marks_lifecycle_tracking_invalid_on_unsupported_schema() -> None:
    tracker = _PiSubspawnTracker.empty()

    tracker.observe(
        _pi_event(
            "meridian.subspawn.start",
            {"schema_version": 2, "subspawn_id": "tracked-1", "wait_policy": "tracked"},
        )
    )
    assert tracker.has_pending() is False
    assert (
        tracker.lifecycle_tracking_invalidated_error
        == "pi_lifecycle_tracking_invalidated:unsupported_schema_version:2"
    )

    tracker.observe(
        _pi_event(
            "meridian.notification.queued",
            {"schema_version": "2", "notification_id": "n-1"},
        )
    )
    assert tracker.has_pending_notifications() is False


def test_pi_subspawn_tracker_ignores_parse_error_diagnostics() -> None:
    tracker = _PiSubspawnTracker.empty()
    tracker.observe(
        _pi_event(
            "meridian.lifecycle.parse_error",
            {
                "type": "meridian.lifecycle.parse_error",
                "reason": "missing_type",
                "raw_line": '{"id":"missing-type"}',
            },
        )
    )
    assert tracker.has_pending() is False
    assert tracker.has_pending_notifications() is False
    assert tracker.notification_failure_error is None
    assert tracker.lifecycle_tracking_invalidated_error is None


def test_pi_subspawn_tracker_invalidates_on_canonical_lifecycle_parse_error() -> None:
    tracker = _PiSubspawnTracker.empty()
    tracker.observe(
        _pi_event(
            "meridian.lifecycle.parse_error",
            {
                "type": "meridian.lifecycle.parse_error",
                "schema_version": 1,
                "reason": "unsupported_schema_version",
                "error": "unsupported_schema_version",
                "raw_type": "meridian.notification.queued",
                "raw_line": '{"type":"meridian.notification.queued","schema_version":2}',
            },
        )
    )
    assert (
        tracker.lifecycle_tracking_invalidated_error
        == "pi_lifecycle_tracking_invalidated:unsupported_schema_event:meridian.notification.queued"
    )


@pytest.mark.asyncio
async def test_spawn_manager_pi_quiescence_stops_spawned_after_notification_completion(
    tmp_path: Path,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.subspawn.start",
            {"subspawn_id": "j-1", "wait_policy": "tracked"},
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event(
            "meridian.subspawn.end",
            {"subspawn_id": "j-1", "wait_policy": "tracked"},
        ),
        _pi_event("meridian.notification.queued", {"notification_id": "n-1"}),
        _pi_event("meridian.notification.delivered", {"notification_id": "n-1"}),
        _pi_event("agent_start", {}),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event("meridian.notification.completed", {"notification_id": "n-1"}),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-quiescent")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.error is None
        assert fake_connection.stop_reasons == ["quiescent"]
        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        waiting_for_children = [
            event
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase") == "waiting_for_tracked_children"
        ]
        waiting_for_notifications = [
            event
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase")
            == "waiting_for_notification_completion"
        ]
        assert waiting_for_children
        assert waiting_for_children[-1]["payload"]["active_tracked_count"] == 1
        assert waiting_for_notifications
        assert waiting_for_notifications[-1]["payload"]["pending_notification_count"] == 1
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_emits_notification_wait_phase_without_prior_terminal(
    tmp_path: Path,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event("meridian.notification.queued", {"notification_id": "n-1"}),
        _pi_event("meridian.notification.delivered", {"notification_id": "n-1"}),
        _pi_event("meridian.notification.completed", {"notification_id": "n-1"}),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-notification-wait-phase")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "succeeded"
        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        waiting_for_notifications = [
            event
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase")
            == "waiting_for_notification_completion"
        ]
        assert waiting_for_notifications
        assert waiting_for_notifications[-1]["payload"]["pending_notification_count"] == 1
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_zero_grace_still_consumes_immediate_notification_wave(
    tmp_path: Path,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.subspawn.start",
            {"subspawn_id": "j-1", "wait_policy": "tracked"},
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event(
            "meridian.subspawn.end",
            {"subspawn_id": "j-1", "wait_policy": "tracked"},
        ),
        _pi_event("meridian.notification.queued", {"notification_id": "n-1"}),
        _pi_event("meridian.notification.delivered", {"notification_id": "n-1"}),
        _pi_event("agent_start", {}),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event("meridian.notification.completed", {"notification_id": "n-1"}),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.0,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-zero-grace-notification-wave")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "succeeded"
        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert any(
            event.get("event_type") == "meridian.notification.queued"
            and event.get("payload", {}).get("notification_id") == "n-1"
            for event in history
        )
        assert any(
            event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase") == "waiting_for_notification_completion"
            for event in history
        )
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_primary_role_does_not_auto_stop_at_quiescence(
    tmp_path: Path,
) -> None:
    class _OpenPrimaryPiConnection(_FakePiConnection):
        def __init__(self, events: list[HarnessEvent]) -> None:
            super().__init__(events)
            self._closed = asyncio.Event()

        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: StopProgressCallback | None = None,
        ) -> StopResult:
            _ = progress
            self.stop_reasons.append(reason)
            self._closed.set()
            self._state = "stopped"
            return StopResult()

        async def events(self):  # type: ignore[no-untyped-def]
            for event in self._events:
                yield event
            await self._closed.wait()

    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event(
            "meridian.quiescence.ready",
            {
                "schema_version": 1,
                "role": "primary",
                "tracked_count": 0,
                "pending_notification_count": 0,
            },
        ),
    ]
    fake_connection = _OpenPrimaryPiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-primary-no-quiescent-stop")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="primary",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(manager.wait_for_completion(spawn_id)),
                timeout=0.05,
            )
        assert fake_connection.stop_reasons == []
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_quiescent_stop_escalation_preserves_success_terminal(
    tmp_path: Path,
) -> None:
    class _EscalatedButSuccessfulStopConnection(_FakePiConnection):
        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: StopProgressCallback | None = None,
        ) -> StopResult:
            self.stop_reasons.append(reason)
            self._state = "stopped"
            if reason == "quiescent" and progress is not None:
                await progress("quiescent_stop_escalating", {"reason": "abort_grace_expired"})
            return StopResult(escalated=reason == "quiescent")

    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
    ]
    fake_connection = _EscalatedButSuccessfulStopConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-quiescent-stop-escalated-success")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.error is None
        assert fake_connection.stop_reasons == ["quiescent"]

        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        escalated_phases = [
            event
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase") == "quiescent_stop_escalated"
        ]
        escalating_phases = [
            event
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase") == "quiescent_stop_escalating"
        ]
        assert escalating_phases
        assert escalating_phases[-1]["payload"].get("reason") == "abort_grace_expired"
        assert escalated_phases
        assert escalated_phases[-1]["payload"].get("reason") == "abort_grace_expired"
        assert history.index(escalating_phases[-1]) < history.index(escalated_phases[-1])
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_quiescent_stop_escalation_marks_spawn_failed(
    tmp_path: Path,
) -> None:
    class _EscalatingStopConnection(_FakePiConnection):
        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: StopProgressCallback | None = None,
        ) -> StopResult:
            _ = progress
            self.stop_reasons.append(reason)
            self._state = "stopped"
            if reason == "quiescent":
                raise RuntimeError("pi_quiescent_stop_escalated")
            return StopResult()

    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
    ]
    fake_connection = _EscalatingStopConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-quiescent-stop-escalated")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_quiescent_stop_failed:pi_quiescent_stop_escalated"
        assert fake_connection.stop_reasons == ["quiescent"]
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_quiescence_grace_waits_for_late_notification_events(
    tmp_path: Path,
) -> None:
    class _DelayedPiConnection(_FakePiConnection):
        def __init__(self, delayed_events: list[tuple[float, HarnessEvent]]) -> None:
            super().__init__([])
            self._delayed_events = delayed_events

        async def events(self):  # type: ignore[no-untyped-def]
            for delay, event in self._delayed_events:
                if delay > 0:
                    await asyncio.sleep(delay)
                yield event

    grace_secs = 0.05
    fake_connection = _DelayedPiConnection(
        [
            (0.0, _pi_event("session", {"id": "ses-pi"})),
            (
                0.0,
                _pi_event(
                    "agent_end",
                    {"messages": [{"role": "assistant", "stopReason": "stop"}]},
                ),
            ),
            (0.02, _pi_event("meridian.notification.queued", {"notification_id": "n-late"})),
            (
                0.0,
                _pi_event("meridian.notification.delivered", {"notification_id": "n-late"}),
            ),
            (0.0, _pi_event("agent_start", {})),
            (
                0.0,
                _pi_event(
                    "agent_end",
                    {"messages": [{"role": "assistant", "stopReason": "stop"}]},
                ),
            ),
            (
                0.0,
                _pi_event("meridian.notification.completed", {"notification_id": "n-late"}),
            ),
        ]
    )

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=grace_secs,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-late-notification-grace")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    start = asyncio.get_running_loop().time()
    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=2.0)
        elapsed = asyncio.get_running_loop().time() - start
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.error is None
        assert elapsed >= grace_secs
        assert fake_connection.stop_reasons == ["quiescent"]
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_fast_child_completion_waits_for_notification_completion(
    tmp_path: Path,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.subspawn.start",
            {"schema_version": 1, "subspawn_id": "j-fast", "wait_policy": "tracked"},
        ),
        _pi_event(
            "meridian.subspawn.end",
            {"schema_version": 1, "subspawn_id": "j-fast", "wait_policy": "tracked"},
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event(
            "meridian.notification.queued",
            {"schema_version": 1, "notification_id": "n-fast"},
        ),
        _pi_event(
            "meridian.notification.delivered",
            {"schema_version": 1, "notification_id": "n-fast"},
        ),
        _pi_event("agent_start", {}),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event(
            "meridian.notification.completed",
            {"schema_version": 1, "notification_id": "n-fast"},
        ),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-fast-child-notification")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.error is None
        assert fake_connection.stop_reasons == ["quiescent"]
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_second_child_wave_reblocks_quiescence(
    tmp_path: Path,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.subspawn.start",
            {"schema_version": 1, "subspawn_id": "j-1", "wait_policy": "tracked"},
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event(
            "meridian.subspawn.end",
            {"schema_version": 1, "subspawn_id": "j-1", "wait_policy": "tracked"},
        ),
        _pi_event(
            "meridian.notification.queued",
            {"schema_version": 1, "notification_id": "n-1"},
        ),
        _pi_event(
            "meridian.notification.delivered",
            {"schema_version": 1, "notification_id": "n-1"},
        ),
        _pi_event("agent_start", {}),
        _pi_event(
            "meridian.subspawn.start",
            {"schema_version": 1, "subspawn_id": "j-2", "wait_policy": "tracked"},
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event(
            "meridian.notification.completed",
            {"schema_version": 1, "notification_id": "n-1"},
        ),
        _pi_event(
            "meridian.subspawn.end",
            {"schema_version": 1, "subspawn_id": "j-2", "wait_policy": "tracked"},
        ),
        _pi_event(
            "meridian.notification.queued",
            {"schema_version": 1, "notification_id": "n-2"},
        ),
        _pi_event(
            "meridian.notification.delivered",
            {"schema_version": 1, "notification_id": "n-2"},
        ),
        _pi_event("agent_start", {}),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event(
            "meridian.notification.completed",
            {"schema_version": 1, "notification_id": "n-2"},
        ),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-second-wave")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.error is None
        assert fake_connection.stop_reasons == ["quiescent"]

        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history_text = history_path.read_text(encoding="utf-8")
        assert '"subspawn_id":"j-2"' in history_text
        assert '"notification_id":"n-2"' in history_text
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_notification_failure_marks_spawn_failed(tmp_path: Path) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event("meridian.notification.queued", {"notification_id": "n-1"}),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
        _pi_event(
            "meridian.notification.failed",
            {
                "notification_id": "n-1",
                "reason": "sendMessage_error",
                "error": "delivery failed",
            },
        ),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-notification-fail")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_notification_failed:sendMessage_error:delivery failed"
        assert fake_connection.stop_reasons == []
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_parse_error_invalidates_quiescence_and_fails(
    tmp_path: Path,
) -> None:
    raw_line = '{"type":"meridian.subspawn.start","schema_version":2}'
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.lifecycle.parse_error",
            {
                "type": "meridian.lifecycle.parse_error",
                "schema_version": 1,
                "reason": "unsupported_schema_version",
                "error": "unsupported_schema_version",
                "raw_type": "meridian.subspawn.start",
                "raw_line": raw_line,
            },
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-unsupported-schema")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "failed"
        assert (
            outcome.error
            == "pi_lifecycle_tracking_invalidated:unsupported_schema_event:meridian.subspawn.start"
        )
        assert fake_connection.stop_reasons == []
        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert any(event["event_type"] == "meridian.lifecycle.parse_error" for event in history)
        assert any(event["payload"].get("raw_line") == raw_line for event in history)
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_fails_on_canonical_subspawn_without_id_for_quiescence(
    tmp_path: Path,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.subspawn.start",
            {"wait_policy": "tracked"},
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-canonical-missing-id")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "failed"
        assert (
            outcome.error
            == "pi_lifecycle_tracking_invalidated:missing_subspawn_id:meridian.subspawn.start"
        )
        assert fake_connection.stop_reasons == []
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_pending_children_runs_posix_cleanup_hook(tmp_path: Path) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.subspawn.start",
            {"subspawn_id": "j-1", "wait_policy": "tracked", "pid": 5401},
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    cleanup_calls: list[tuple[int, str]] = []

    async def _fake_cleanup(*, spawn_id: SpawnId, process_group_id: int, reason: str) -> None:
        _ = spawn_id
        cleanup_calls.append((process_group_id, reason))

    manager._terminate_posix_process_group = _fake_cleanup  # type: ignore[method-assign]

    spawn_id = SpawnId("p-pi-pending-child-cleanup")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_process_exited_with_tracked_children"
        assert cleanup_calls == [(5401, "pi_process_exit_with_tracked_children")]
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_failed_terminal_does_not_defer_with_pending_children(
    tmp_path: Path,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.subspawn.start",
            {"subspawn_id": "j-1", "wait_policy": "tracked", "pid": 6501},
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "error"}]},
        ),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    cleanup_calls: list[tuple[int, str]] = []

    async def _fake_cleanup(*, spawn_id: SpawnId, process_group_id: int, reason: str) -> None:
        _ = spawn_id
        cleanup_calls.append((process_group_id, reason))

    manager._terminate_posix_process_group = _fake_cleanup  # type: ignore[method-assign]

    spawn_id = SpawnId("p-pi-failed-terminal-pending-child")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_stop_error"
        assert cleanup_calls == [(6501, "pi_process_exit_with_tracked_children")]
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_cancelled_terminal_cleans_up_pending_children(
    tmp_path: Path,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.subspawn.start",
            {"subspawn_id": "j-1", "wait_policy": "tracked", "pid": 6601},
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "cancelled"}]},
        ),
    ]
    fake_connection = _FakePiConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    cleanup_calls: list[tuple[int, str]] = []

    async def _fake_cleanup(*, spawn_id: SpawnId, process_group_id: int, reason: str) -> None:
        _ = spawn_id
        cleanup_calls.append((process_group_id, reason))

    manager._terminate_posix_process_group = _fake_cleanup  # type: ignore[method-assign]

    spawn_id = SpawnId("p-pi-cancelled-terminal-pending-child")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "cancelled"
        assert outcome.error == "cancelled"
        assert cleanup_calls == [(6601, "pi_process_exit_with_tracked_children")]
        assert fake_connection.stop_reasons == []
    finally:
        await manager.stop_spawn(spawn_id)
