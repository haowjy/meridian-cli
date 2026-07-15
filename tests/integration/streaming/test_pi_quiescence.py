# qa-validated: pi-rpc-quiescence
"""Pi RPC quiescence policy and tracker tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import (
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.state import spawn_store
from meridian.lib.streaming import pi_drain as pi_drain_module
from meridian.lib.streaming.pi_subspawn_tracker import PiSubspawnTracker
from tests.support.async_determinism import (
    AsyncDeterminism,
    TaskGate,
    assert_still_pending,
    wait_until,
    yield_to_loop,
)
from tests.support.pi import (
    FakePiConnection as _FakePiConnection,
)
from tests.support.pi import (
    pi_event as _pi_event,
)
from tests.support.pi import (
    start_pi_manager as _start_pi_manager,
)


def _read_history(runtime_root: Path, spawn_id: SpawnId) -> list[dict[str, Any]]:
    history_path = runtime_root / "spawns" / str(spawn_id) / "history.jsonl"
    return [
        json.loads(line)
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _read_history_phases(runtime_root: Path, spawn_id: SpawnId) -> list[str]:
    return [
        cast("str", event.get("payload", {}).get("phase"))
        for event in _read_history(runtime_root, spawn_id)
        if event.get("event_type") == "meridian.pi.lifecycle.phase"
    ]


def _history_has_phase(runtime_root: Path, spawn_id: SpawnId, phase: str) -> bool:
    history_path = runtime_root / "spawns" / str(spawn_id) / "history.jsonl"
    return history_path.exists() and phase in _read_history_phases(runtime_root, spawn_id)


def _history_has_event(runtime_root: Path, spawn_id: SpawnId, event_type: str) -> bool:
    history_path = runtime_root / "spawns" / str(spawn_id) / "history.jsonl"
    return history_path.exists() and any(
        event.get("event_type") == event_type
        for event in _read_history(runtime_root, spawn_id)
    )


def _read_phase_events(
    runtime_root: Path,
    spawn_id: SpawnId,
    phase: str,
) -> list[dict[str, Any]]:
    return [
        event
        for event in _read_history(runtime_root, spawn_id)
        if event.get("event_type") == "meridian.pi.lifecycle.phase"
        and event.get("payload", {}).get("phase") == phase
    ]


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

    spawn_id = SpawnId("p-pi-quiescent")
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=spawn_id,
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.error is None
        assert fake_connection.stop_reasons == []
        waiting_for_children = _read_phase_events(
            tmp_path,
            spawn_id,
            "waiting_for_tracked_children",
        )
        waiting_for_notifications = _read_phase_events(
            tmp_path,
            spawn_id,
            "waiting_for_notification_completion",
        )
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

    spawn_id = SpawnId("p-pi-primary-no-quiescent-stop")
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=spawn_id,
        session_role="primary",
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

    spawn_id = SpawnId("p-pi-quiescent-stop-escalated-success")
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=spawn_id,
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.error is None
        assert fake_connection.stop_reasons == []

        await wait_until(
            lambda: "cleanup_escalated"
            in _read_history_phases(tmp_path, spawn_id),
            description="cleanup_escalated lifecycle phase",
        )
        history = _read_history(tmp_path, spawn_id)
        cleanup_escalated_phases = _read_phase_events(
            tmp_path,
            spawn_id,
            "cleanup_escalated",
        )
        cleanup_running_phases = _read_phase_events(
            tmp_path,
            spawn_id,
            "cleanup_running",
        )
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

    spawn_id = SpawnId("p-pi-micro-drain-bounded-timeout")
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=spawn_id,
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.error is None

        phases = _read_history_phases(tmp_path, spawn_id)
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
    determinism = AsyncDeterminism(start=100.0)
    monkeypatch.setattr(pi_drain_module.time, "monotonic", determinism.clock.monotonic)
    loop = asyncio.get_running_loop()
    real_loop_time = loop.time
    determinism.install_on_running_loop(monkeypatch)
    calls: list[tuple[object, ...]] = []
    parent_id = SpawnId("p-pi-hybrid-reap")
    child_id = SpawnId("p-child-hybrid-reap")

    class _Service:
        async def cancel_descendants(self, target_spawn_id: SpawnId) -> set[str]:
            calls.append(("cancel_descendants", str(target_spawn_id)))
            child = spawn_store.get_spawn(tmp_path, child_id)
            assert child is not None
            assert child.parent_id == str(parent_id)
            assert child.status == "running"
            if cancel_raises:
                raise RuntimeError("cancel failed")
            return {str(child_id)}

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
            await asyncio.Event().wait()

    fake_connection = _StuckWaveTimeoutConnection(
        [
            _pi_event("session", {"id": "ses-pi"}),
            _pi_event(
                "meridian.subspawn.start",
                {
                    "schema_version": 1,
                    "subspawn_id": str(child_id),
                    "correlation_id": str(child_id),
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

    spawn_store.start_spawn(
        tmp_path,
        spawn_id=child_id,
        chat_id=str(child_id),
        parent_id=str(parent_id),
        model="test-model",
        agent="test-agent",
        harness="pi",
        prompt="child",
        status="running",
    )
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=parent_id,
        child_wave_timeout_seconds=0.02,
    )

    completion = asyncio.create_task(manager.wait_for_completion(parent_id))
    try:
        await wait_until(
            lambda: _history_has_event(tmp_path, parent_id, "agent_end"),
            description="Pi child-wave wait",
        )
        await assert_still_pending(completion)
        determinism.advance(0.019)
        await yield_to_loop()
        assert not completion.done()
        determinism.advance(0.0011)
        await wait_until(completion.done, description="Pi child-wave timeout")
        outcome = await completion
    finally:
        monkeypatch.setattr(loop, "time", real_loop_time)
        await manager.stop_spawn(parent_id)
    assert outcome is not None
    return calls, outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_raises", "fallback_pids"),
    ((False, (8801,)), (True, (7701, 8801))),
    ids=("descendants-reaped", "descendant-reap-failed"),
)
async def test_spawn_manager_pi_reap_falls_back_for_unreaped_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_raises: bool,
    fallback_pids: tuple[int, ...],
) -> None:
    calls, outcome = await _run_pi_child_wave_timeout_with_cleanup_mocks(
        tmp_path,
        monkeypatch,
        cancel_raises=cancel_raises,
    )

    assert outcome.status == "failed"
    assert outcome.error == "pi_child_wave_timeout"
    assert calls == [
        ("cancel_descendants", "p-pi-hybrid-reap"),
        ("pgid_fallback", "p-pi-hybrid-reap", "pi_child_wave_timeout", fallback_pids),
    ]


@pytest.mark.asyncio
async def test_spawn_manager_pi_child_wave_timeout_cleans_tracked_children_and_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    determinism = AsyncDeterminism(start=100.0)
    monkeypatch.setattr(pi_drain_module.time, "monotonic", determinism.clock.monotonic)
    loop = asyncio.get_running_loop()
    real_loop_time = loop.time
    determinism.install_on_running_loop(monkeypatch)
    class _StuckWaveTimeoutConnection(_FakePiConnection):
        async def events(self):  # type: ignore[no-untyped-def]
            for event in self._events:
                yield event
            await asyncio.Event().wait()

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

    spawn_id = SpawnId("p-pi-child-wave-timeout")
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=spawn_id,
        child_wave_timeout_seconds=0.02,
    )

    completion = asyncio.create_task(manager.wait_for_completion(spawn_id))
    try:
        await wait_until(
            lambda: _history_has_phase(tmp_path, spawn_id, "quiescence_deferred"),
            description="Pi child-wave wait",
        )
        await assert_still_pending(completion)
        determinism.advance(0.019)
        await yield_to_loop()
        assert not completion.done()
        determinism.advance(0.0011)
        await wait_until(completion.done, description="Pi child-wave timeout")
        outcome = await completion
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_child_wave_timeout"
        timeout_events = _read_phase_events(
            tmp_path,
            spawn_id,
            "pi_child_wave_timeout",
        )
        assert timeout_events
        assert timeout_events[-1]["payload"].get("active_tracked_count") == 1
    finally:
        monkeypatch.setattr(loop, "time", real_loop_time)
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_pi_child_wave_timeout_not_cleared_by_turn_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After expiry is observed, later activity cannot revive the child wave."""
    determinism = AsyncDeterminism(start=100.0)
    monkeypatch.setattr(pi_drain_module.time, "monotonic", determinism.clock.monotonic)
    loop = asyncio.get_running_loop()
    real_loop_time = loop.time
    determinism.install_on_running_loop(monkeypatch)
    late_turn_gate = TaskGate()

    class _DelayedWaveTimeoutConnection(_FakePiConnection):
        def __init__(self, delayed_events: list[tuple[float, HarnessEvent]]) -> None:
            super().__init__([])
            self._delayed_events = delayed_events

        async def events(self):  # type: ignore[no-untyped-def]
            for delay, event in self._delayed_events:
                if delay > 0:
                    await late_turn_gate.wait_open()
                yield event
            await asyncio.Event().wait()

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

    spawn_id = SpawnId("p-pi-child-wave-timeout-turn-active")
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=spawn_id,
        child_wave_timeout_seconds=0.1,
    )

    async def _release_late_turn_after_child_wave_timeout() -> None:
        await wait_until(
            lambda: (
                (tmp_path / "spawns" / str(spawn_id) / "history.jsonl").exists()
                and "pi_child_wave_timeout"
                in _read_history_phases(tmp_path, spawn_id)
            ),
            description="child-wave timeout phase before late turn_active",
        )
        late_turn_gate.open()

    release_task = asyncio.create_task(_release_late_turn_after_child_wave_timeout())
    completion = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await wait_until(
            lambda: _history_has_phase(tmp_path, spawn_id, "quiescence_deferred"),
            description="Pi child-wave wait",
        )
        await assert_still_pending(completion)
        determinism.advance(0.099)
        await yield_to_loop()
        assert not completion.done()
        determinism.advance(0.0011)
        await wait_until(completion.done, description="latched Pi child-wave timeout")
        outcome = await completion
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_child_wave_timeout"
        assert _read_history_phases(tmp_path, spawn_id).count("pi_child_wave_timeout") == 1
    finally:
        release_task.cancel()
        with suppress(asyncio.CancelledError):
            await release_task
        monkeypatch.setattr(loop, "time", real_loop_time)
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

    spawn_id = SpawnId("p-pi-notification-fail")
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=spawn_id,
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
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

    spawn_id = SpawnId("p-pi-unsupported-schema")
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=spawn_id,
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "failed"
        assert (
            outcome.error
            == "pi_lifecycle_tracking_invalidated:unsupported_schema_event:meridian.subspawn.start"
        )
        assert fake_connection.stop_reasons == []
        history = _read_history(tmp_path, spawn_id)
        assert any(event["event_type"] == "meridian.lifecycle.parse_error" for event in history)
        assert any(event["payload"].get("raw_line") == raw_line for event in history)
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "lifecycle_event", "expected_error"),
    (
        (
            "legacy-event",
            _pi_event(
                "meridian_subspawn_started",
                {"id": "legacy-child", "wait_policy": "tracked", "pid": 4401},
            ),
            "pi_lifecycle_tracking_invalidated:unsupported_lifecycle_event:"
            "meridian_subspawn_started",
        ),
        (
            "missing-id",
            _pi_event("meridian.subspawn.start", {"wait_policy": "tracked"}),
            "pi_lifecycle_tracking_invalidated:missing_subspawn_id:"
            "meridian.subspawn.start",
        ),
    ),
    ids=("legacy-event", "missing-id"),
)
async def test_spawn_manager_pi_invalid_lifecycle_event_fails_spawn(
    tmp_path: Path,
    case: str,
    lifecycle_event: HarnessEvent,
    expected_error: str,
) -> None:
    events = [
        _pi_event("session", {"id": "ses-pi"}),
        lifecycle_event,
        _pi_event(
            "agent_end",
            {"messages": [{"role": "assistant", "stopReason": "stop"}]},
        ),
    ]
    fake_connection = _FakePiConnection(events)

    spawn_id = SpawnId(f"p-pi-{case}")
    manager = await _start_pi_manager(
        tmp_path,
        fake_connection,
        spawn_id=spawn_id,
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == expected_error
        assert fake_connection.stop_reasons == []
    finally:
        await manager.stop_spawn(spawn_id)
