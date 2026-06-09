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
    ConnectionConfig,
    HarnessConnection,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.streaming.pi_subspawn_tracker import PiSubspawnTracker
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.unit.streaming.pi_quiescence_test_helpers import (
    FakePiConnection as _FakePiConnection,
)
from tests.unit.streaming.pi_quiescence_test_helpers import (
    NoopControlServer as _NoopControlServer,
)
from tests.unit.streaming.pi_quiescence_test_helpers import (
    pi_event as _pi_event,
)


def test_pi_subspawn_tracker_tracks_only_blocking_children_and_notifications() -> None:
    tracker = PiSubspawnTracker.empty()

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
            {"subspawn_id": "tracked-1", "wait_policy": "tracked", "pid": 4401},
        )
    )
    assert tracker.has_pending() is True
    assert tracker.active_tracked_pgid_candidates() == (4401,)

    tracker.observe(_pi_event("meridian.notification.queued", {"notification_id": "n-1"}))
    assert tracker.has_pending_notifications() is True

    tracker.observe(_pi_event("meridian.notification.completed", {"notification_id": "n-1"}))
    assert tracker.has_pending_notifications() is False

    tracker.observe(
        _pi_event(
            "meridian.notification.failed",
            {"notification_id": "n-2", "reason": "sendMessage_error"},
        )
    )
    assert tracker.notification_failure_error == "pi_notification_failed:sendMessage_error"

    tracker.observe(
        _pi_event("meridian.subspawn.end", {"subspawn_id": "tracked-1", "wait_policy": "tracked"})
    )
    assert tracker.has_pending() is False
    assert tracker.active_tracked_pgid_candidates() == ()


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_error"),
    [
        (
            "meridian.subspawn.start",
            {"wait_policy": "tracked"},
            "pi_lifecycle_tracking_invalidated:missing_subspawn_id:meridian.subspawn.start",
        ),
        (
            "meridian.subspawn.start",
            {"schema_version": 2, "subspawn_id": "tracked-1", "wait_policy": "tracked"},
            "pi_lifecycle_tracking_invalidated:unsupported_schema_version:2",
        ),
        (
            "meridian.lifecycle.parse_error",
            {
                "type": "meridian.lifecycle.parse_error",
                "schema_version": 1,
                "reason": "unsupported_schema_version",
                "error": "unsupported_schema_version",
                "raw_type": "meridian.subspawn.start",
                "raw_line": '{"type":"meridian.subspawn.start","schema_version":2}',
            },
            "pi_lifecycle_tracking_invalidated:unsupported_schema_event:meridian.subspawn.start",
        ),
    ],
)
def test_pi_subspawn_tracker_invalidates_malformed_canonical_lifecycle(
    event_type: str,
    payload: dict[str, object],
    expected_error: str,
) -> None:
    tracker = PiSubspawnTracker.empty()

    tracker.observe(_pi_event(event_type, payload))

    assert tracker.has_pending() is False
    assert tracker.has_pending_notifications() is False
    assert tracker.lifecycle_tracking_invalidated_error == expected_error


def test_pi_subspawn_tracker_ignores_noncanonical_parse_diagnostics() -> None:
    tracker = PiSubspawnTracker.empty()

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


def test_pi_subspawn_tracker_invalidates_unknown_pi_lifecycle_namespace_events() -> None:
    tracker = PiSubspawnTracker.empty()

    tracker.observe(
        _pi_event(
            "meridian_subspawn_started",
            {"id": "legacy-child", "wait_policy": "tracked", "pid": 4401},
        )
    )

    assert tracker.has_pending() is False
    assert tracker.has_pending_notifications() is False
    assert (
        tracker.lifecycle_tracking_invalidated_error
        == "pi_lifecycle_tracking_invalidated:unsupported_lifecycle_event:"
        "meridian_subspawn_started"
    )


def test_pi_subspawn_tracker_leaves_ordinary_harness_events_unaffected() -> None:
    tracker = PiSubspawnTracker.empty()

    tracker.observe(_pi_event("agent_progress", {"message": "ordinary output"}))

    assert tracker.has_pending() is False
    assert tracker.has_pending_notifications() is False
    assert tracker.notification_failure_error is None
    assert tracker.lifecycle_tracking_invalidated_error is None


def test_pi_subspawn_tracker_deduplicates_canonical_events() -> None:
    tracker = PiSubspawnTracker.empty()

    start = _pi_event(
        "meridian.subspawn.start",
        {
            "schema_version": 1,
            "subspawn_id": "j-dup",
            "correlation_id": "corr-start",
            "wait_policy": "tracked",
            "pid": 4401,
        },
    )
    duplicate_start = _pi_event(
        "meridian.subspawn.start",
        {
            "schema_version": 1,
            "subspawn_id": "j-dup",
            "correlation_id": "corr-start",
            "wait_policy": "tracked",
            "pid": 5501,
        },
    )
    end = _pi_event(
        "meridian.subspawn.end",
        {
            "schema_version": 1,
            "subspawn_id": "j-dup",
            "correlation_id": "corr-end",
            "wait_policy": "tracked",
        },
    )

    assert tracker.observe(start) is False
    assert tracker.observe(duplicate_start) is True
    assert tracker.active_tracked_count() == 1
    assert tracker.active_tracked_pgid_candidates() == (4401,)
    assert tracker.observe(end) is False
    assert tracker.observe(end) is True
    assert tracker.has_pending() is False
    assert tracker.active_tracked_pgid_candidates() == ()


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
        assert fake_connection.stop_reasons == []
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
            and event.get("payload", {}).get("phase") == "waiting_for_notification_completion"
        ]
        assert waiting_for_children
        assert waiting_for_children[-1]["payload"]["active_tracked_count"] == 1
        assert waiting_for_notifications
        assert waiting_for_notifications[-1]["payload"]["pending_notification_count"] == 1
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
async def test_spawn_manager_pi_cleanup_escalation_does_not_block_terminal_success(
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
        assert fake_connection.stop_reasons == []
        await asyncio.sleep(0.05)

        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        cleanup_escalated_phases = [
            event
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase") == "cleanup_escalated"
        ]
        cleanup_running_phases = [
            event
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase") == "cleanup_running"
        ]
        assert cleanup_running_phases
        assert cleanup_escalated_phases
        assert cleanup_escalated_phases[-1]["payload"].get("reason") == "abort_grace_expired"
        assert history.index(cleanup_running_phases[-1]) < history.index(
            cleanup_escalated_phases[-1]
        )
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_micro_drain_resolves_with_bounded_timeout(
    tmp_path: Path,
) -> None:
    class _OpenAfterTerminalConnection(_FakePiConnection):
        async def events(self):  # type: ignore[no-untyped-def]
            yield _pi_event("session", {"id": "ses-pi"})
            yield _pi_event("turn_end", {"type": "turn_end"})
            yield _pi_event(
                "agent_end",
                {"messages": [{"role": "assistant", "stopReason": "stop"}]},
            )
            await asyncio.sleep(60)

    fake_connection = _OpenAfterTerminalConnection([])

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-micro-drain-bounded-timeout")
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

        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        phases = [
            event.get("payload", {}).get("phase")
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
        ]
        assert "quiescence_micro_drain_started" in phases
        assert "finalized" in phases
    finally:
        await manager.stop_spawn(spawn_id)


async def _run_pi_child_wave_timeout_with_cleanup_mocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancel_raises: bool = False,
) -> tuple[list[tuple[object, ...]], Any]:
    calls: list[tuple[object, ...]] = []

    class _Service:
        async def cancel_descendants(self, target_spawn_id: SpawnId) -> set[str]:
            calls.append(("cancel_descendants", str(target_spawn_id)))
            if cancel_raises:
                raise RuntimeError("cancel failed")
            return {"j-hybrid-reap"}

    def _build_service(project_root: Path, runtime_root: Path) -> _Service:
        assert project_root == tmp_path
        assert runtime_root == tmp_path
        return _Service()

    async def _fallback_cleanup(
        target_spawn_id: SpawnId,
        tracker: PiSubspawnTracker,
        *,
        reason: str,
        exclude_subspawn_ids: set[str] | None = None,
    ) -> None:
        calls.append(
            (
                "pgid_fallback",
                str(target_spawn_id),
                reason,
                tracker.active_tracked_pgid_candidates(exclude_ids=exclude_subspawn_ids),
            )
        )

    monkeypatch.setattr(
        "meridian.lib.bootstrap.services.build_spawn_application_service_from_roots",
        _build_service,
    )
    monkeypatch.setattr(
        "meridian.lib.streaming.spawn_manager.terminate_pi_tracked_subspawns",
        _fallback_cleanup,
    )

    class _StuckWaveTimeoutConnection(_FakePiConnection):
        async def events(self):  # type: ignore[no-untyped-def]
            for event in self._events:
                yield event
            await asyncio.sleep(60)

    fake_connection = _StuckWaveTimeoutConnection(
        [
            _pi_event("session", {"id": "ses-pi"}),
            _pi_event(
                "meridian.subspawn.start",
                {
                    "schema_version": 1,
                    "subspawn_id": "j-hybrid-reap",
                    "correlation_id": "j-hybrid-reap",
                    "wait_policy": "tracked",
                    "pid": 7701,
                },
            ),
            _pi_event(
                "meridian.subspawn.start",
                {
                    "schema_version": 1,
                    "subspawn_id": "pi-internal-managed-bash",
                    "correlation_id": "pi-internal-managed-bash",
                    "wait_policy": "tracked",
                    "pid": 8801,
                },
            ),
            _pi_event(
                "agent_end",
                {"messages": [{"role": "assistant", "stopReason": "stop"}]},
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
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )
    spawn_id = SpawnId("p-pi-hybrid-reap")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
            pi_child_wave_timeout_seconds=0.02,
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
    finally:
        await manager.stop_spawn(spawn_id)
    assert outcome is not None
    return calls, outcome


@pytest.mark.asyncio
async def test_spawn_manager_pi_reap_cancels_descendants_before_pgid_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, outcome = await _run_pi_child_wave_timeout_with_cleanup_mocks(
        tmp_path,
        monkeypatch,
    )

    assert outcome.status == "failed"
    assert outcome.error == "pi_child_wave_timeout"
    assert calls == [
        ("cancel_descendants", "p-pi-hybrid-reap"),
        ("pgid_fallback", "p-pi-hybrid-reap", "pi_child_wave_timeout", (8801,)),
    ]


@pytest.mark.asyncio
async def test_spawn_manager_pi_reap_runs_pgid_fallback_when_cancel_descendants_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, outcome = await _run_pi_child_wave_timeout_with_cleanup_mocks(
        tmp_path,
        monkeypatch,
        cancel_raises=True,
    )

    assert outcome.status == "failed"
    assert outcome.error == "pi_child_wave_timeout"
    assert calls == [
        ("cancel_descendants", "p-pi-hybrid-reap"),
        ("pgid_fallback", "p-pi-hybrid-reap", "pi_child_wave_timeout", (7701, 8801)),
    ]


@pytest.mark.asyncio
async def test_spawn_manager_pi_child_wave_timeout_cleans_tracked_children_and_fails(
    tmp_path: Path,
) -> None:
    class _StuckWaveTimeoutConnection(_FakePiConnection):
        async def events(self):  # type: ignore[no-untyped-def]
            for event in self._events:
                yield event
            await asyncio.sleep(60)

    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian.subspawn.start",
            {
                "schema_version": 1,
                "subspawn_id": "j-wave-timeout",
                "correlation_id": "j-wave-timeout",
                "wait_policy": "tracked",
                "pid": 7701,
            },
        ),
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
    ]
    fake_connection = _StuckWaveTimeoutConnection(events)

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> HarnessConnection[Any]:
        await fake_connection.start(config, spec)
        return fake_connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-child-wave-timeout")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
            pi_child_wave_timeout_seconds=0.02,
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
        assert outcome.error == "pi_child_wave_timeout"
        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        timeout_events = [
            event
            for event in history
            if event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase") == "pi_child_wave_timeout"
        ]
        assert timeout_events
        assert timeout_events[-1]["payload"].get("active_tracked_count") == 1
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_child_wave_timeout_not_cleared_by_turn_active(
    tmp_path: Path,
) -> None:
    class _DelayedWaveTimeoutConnection(_FakePiConnection):
        def __init__(self, delayed_events: list[tuple[float, HarnessEvent]]) -> None:
            super().__init__([])
            self._delayed_events = delayed_events

        async def events(self):  # type: ignore[no-untyped-def]
            for delay, event in self._delayed_events:
                if delay > 0:
                    await asyncio.sleep(delay)
                yield event
            await asyncio.sleep(60)

    fake_connection = _DelayedWaveTimeoutConnection(
        [
            (0.0, _pi_event("session", {"id": "ses-pi"})),
            (
                0.0,
                _pi_event(
                    "meridian.subspawn.start",
                    {
                        "schema_version": 1,
                        "subspawn_id": "j-wave-timeout-latch",
                        "correlation_id": "corr-wave-timeout-latch",
                        "wait_policy": "tracked",
                        "pid": 8801,
                    },
                ),
            ),
            (
                0.0,
                _pi_event(
                    "agent_end",
                    {"messages": [{"role": "assistant", "stopReason": "stop"}]},
                ),
            ),
            (0.15, _pi_event("agent_start", {})),
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
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-child-wave-timeout-turn-active")
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
            pi_child_wave_timeout_seconds=0.1,
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        # This scenario intentionally waits for the child-wave timeout, an unrelated
        # turn_active event, and the follow-up timeout; the outer wait_for is only a
        # hung-test safety net, not the behavior under assertion.
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=2.0)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_child_wave_timeout"
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
async def test_spawn_manager_pi_legacy_lifecycle_event_invalidates_instead_of_finalizing(
    tmp_path: Path,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        _pi_event(
            "meridian_subspawn_started",
            {"id": "legacy-child", "wait_policy": "tracked", "pid": 4401},
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
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    spawn_id = SpawnId("p-pi-legacy-lifecycle")
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
            == "pi_lifecycle_tracking_invalidated:unsupported_lifecycle_event:"
            "meridian_subspawn_started"
        )
        assert fake_connection.stop_reasons == []
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
