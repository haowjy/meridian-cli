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
from tests.support.pi import event_phases, phase_events, wait_for_phase
from tests.support.pi import (
    pi_event as _pi_event,
)
from tests.support.pi import (
    start_pi_manager as _start_pi_manager,
)


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
    observed: list[RawHarnessEvent] = []
    manager = await _start_pi_manager(tmp_path, connection, spawn_id=spawn_id, observed=observed)

    outcome = await manager.stop_spawn(
        spawn_id,
        status="timed_out",
        exit_code=3,
        error="timeout",
    )

    assert outcome is not None
    assert (outcome.status, outcome.exit_code, outcome.error) == ("timed_out", 3, "timeout")
    finalized = phase_events(observed, "finalized")
    assert finalized[-1].payload["status"] == "timed_out"
    assert finalized[-1].payload["exit_code"] == 3
    assert finalized[-1].payload["error"] == "timeout"
    assert connection.stop_reasons == ["stop_spawn", "quiescent"]
    assert event_phases(observed)[-2:] == [
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
        _pi_event("agent_settled", {"aborted": False}),
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
@pytest.mark.parametrize(
    ("stop_error", "escalated", "expected_phases"),
    [
        (None, False, ["cleanup_running", "cleanup_completed"]),
        ("stop failed", False, ["cleanup_running", "cleanup_failed"]),
        (
            None,
            True,
            ["cleanup_running", "cleanup_escalated", "cleanup_completed"],
        ),
    ],
)
async def test_spawn_manager_pi_cleanup_publishes_terminal_before_async_teardown(
    tmp_path: Path,
    stop_error: str | None,
    escalated: bool,
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
            self.stop_reasons.append(reason)
            stop_started.set()
            await allow_stop.wait()
            if stop_error is not None:
                raise RuntimeError(stop_error)
            if escalated and progress is not None:
                await progress("quiescent_stop_escalating", {"reason": "abort_grace_expired"})
            self._state = "stopped"
            return StopResult(escalated=escalated)

    fake_connection = _GatedStopConnection(
        [
            _pi_event("session", {"id": "ses-pi"}),
            _pi_event(
                "agent_end",
                {"messages": [{"role": "assistant", "stopReason": "stop"}]},
            ),
            _pi_event("agent_settled", {"aborted": False}),
        ]
    )
    suffix = "escalated" if escalated else "failed" if stop_error else "completed"
    spawn_id = SpawnId(f"p-pi-cleanup-{suffix}")
    observed: list[RawHarnessEvent] = []
    manager = await _start_pi_manager(
        tmp_path, fake_connection, spawn_id=spawn_id, observed=observed
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.error is None
        await asyncio.wait_for(stop_started.wait(), timeout=1.0)
        assert fake_connection.stop_reasons == ["quiescent"]

        allow_stop.set()
        final_phase = expected_phases[-1]
        await wait_for_phase(observed, final_phase)
        history = observed
        cleanup_events = [
            event
            for event in history
            if event.event_type == "meridian.pi.lifecycle.phase"
            and str(event.payload["phase"]).startswith("cleanup_")
        ]
        assert [event.payload["phase"] for event in cleanup_events] == expected_phases
        if stop_error is not None:
            assert cleanup_events[-1].payload["error"] == stop_error
        if escalated:
            assert cleanup_events[-2].payload.get("reason") == "abort_grace_expired"
    finally:
        allow_stop.set()
        await manager.stop_spawn(spawn_id)
