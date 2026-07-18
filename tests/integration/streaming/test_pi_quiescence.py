# qa-validated: pi-rpc-quiescence
"""Pi RPC quiescence policy and tracker tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import (
    RawHarnessEvent,
    StopProgressCallback,
    StopResult,
)
from tests.support.pi import (
    FakePiConnection as _FakePiConnection,
)
from tests.support.pi import (
    history_has_event,
    history_has_phase,
    read_history,
    read_history_phases,
    read_phase_events,
    wait_for_history_phase,
)
from tests.support.pi import (
    pi_event as _pi_event,
)
from tests.support.pi import (
    start_pi_manager as _start_pi_manager,
)

_read_history = read_history
_read_history_phases = read_history_phases
_wait_for_history_phase = wait_for_history_phase
_history_has_phase = history_has_phase
_history_has_event = history_has_event
_read_phase_events = read_phase_events


@pytest.mark.asyncio
async def test_spawn_manager_pi_attempt_timeout_keeps_terminal_truth_through_cleanup(
    tmp_path: Path,
) -> None:
    stream_closed = asyncio.Event()

    class _TimeoutStopConnection(_FakePiConnection):
        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: StopProgressCallback | None = None,
        ) -> StopResult:
            _ = progress
            self.stop_reasons.append(reason)
            stream_closed.set()
            self._state = "stopped"
            return StopResult()

        async def events(self):  # type: ignore[no-untyped-def]
            await stream_closed.wait()
            yield _pi_event(
                "agent_end",
                {"messages": [{"role": "assistant", "stopReason": "aborted"}]},
            )

    spawn_id = SpawnId("p-pi-attempt-timeout-cleanup")
    connection = _TimeoutStopConnection([])
    manager = await _start_pi_manager(tmp_path, connection, spawn_id=spawn_id)

    outcome = await manager.stop_spawn(
        spawn_id,
        status="timed_out",
        exit_code=3,
        error="timeout",
    )

    assert outcome is not None
    assert (outcome.status, outcome.exit_code, outcome.error) == ("timed_out", 3, "timeout")
    finalized = _read_phase_events(tmp_path, spawn_id, "finalized")
    assert finalized[-1]["payload"]["status"] == "timed_out"
    assert finalized[-1]["payload"]["exit_code"] == 3
    assert finalized[-1]["payload"]["error"] == "timeout"
    assert connection.stop_reasons == ["stop_spawn", "quiescent"]
    assert _read_history_phases(tmp_path, spawn_id)[-2:] == [
        "cleanup_running",
        "cleanup_completed",
    ]




@pytest.mark.asyncio
async def test_spawn_manager_pi_primary_role_does_not_auto_stop_at_quiescence(
    tmp_path: Path,
) -> None:
    class _OpenPrimaryPiConnection(_FakePiConnection):
        def __init__(self, events: list[RawHarnessEvent]) -> None:
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

        await _wait_for_history_phase(tmp_path, spawn_id, "cleanup_completed")
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
        cleanup_phases = [
            event["payload"]["phase"]
            for event in history
            if event["event_type"] == "meridian.pi.lifecycle.phase"
            and str(event["payload"]["phase"]).startswith("cleanup_")
        ]
        assert cleanup_phases == [
            "cleanup_running",
            "cleanup_escalated",
            "cleanup_completed",
        ]
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_error", "expected_phases"),
    [
        (None, ["cleanup_running", "cleanup_completed"]),
        ("stop failed", ["cleanup_running", "cleanup_failed"]),
    ],
)
async def test_spawn_manager_pi_cleanup_publishes_terminal_before_async_teardown(
    tmp_path: Path,
    stop_error: str | None,
    expected_phases: list[str],
) -> None:
    stop_started = asyncio.Event()
    allow_stop = asyncio.Event()

    class _GatedStopConnection(_FakePiConnection):
        async def stop(
            self,
            *,
            reason: str | None = None,
            progress: StopProgressCallback | None = None,
        ) -> StopResult:
            _ = progress
            self.stop_reasons.append(reason)
            stop_started.set()
            await allow_stop.wait()
            if stop_error is not None:
                raise RuntimeError(stop_error)
            self._state = "stopped"
            return StopResult()

    fake_connection = _GatedStopConnection(
        [
            _pi_event("session", {"id": "ses-pi"}),
            _pi_event(
                "agent_end",
                {"messages": [{"role": "assistant", "stopReason": "stop"}]},
            ),
        ]
    )
    spawn_id = SpawnId(f"p-pi-cleanup-{'failed' if stop_error else 'completed'}")
    manager = await _start_pi_manager(tmp_path, fake_connection, spawn_id=spawn_id)

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "succeeded"
        await asyncio.wait_for(stop_started.wait(), timeout=1.0)
        assert fake_connection.stop_reasons == ["quiescent"]

        allow_stop.set()
        final_phase = expected_phases[-1]
        await _wait_for_history_phase(tmp_path, spawn_id, final_phase)
        history = _read_history(tmp_path, spawn_id)
        cleanup_events = [
            event
            for event in history
            if event["event_type"] == "meridian.pi.lifecycle.phase"
            and str(event["payload"]["phase"]).startswith("cleanup_")
        ]
        assert [event["payload"]["phase"] for event in cleanup_events] == expected_phases
        if stop_error is not None:
            assert cleanup_events[-1]["payload"]["error"] == stop_error
    finally:
        allow_stop.set()
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

        await _wait_for_history_phase(tmp_path, spawn_id, "finalized")
        phases = _read_history_phases(tmp_path, spawn_id)
        assert "quiescence_micro_drain_started" in phases
        assert "finalized" in phases
    finally:
        await manager.stop_spawn(spawn_id)
