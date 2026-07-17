"""Connection teardown strategy behavior."""

from __future__ import annotations

from typing import Any

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import HarnessEvent, StopResult
from meridian.lib.streaming.drain_teardown import (
    DefaultDrainSessionTeardown,
    PiDrainSessionTeardown,
)
from meridian.lib.streaming.spawn_session import DrainOutcome


class _StopConnection:
    harness_id = HarnessId.PI

    def __init__(self, *, escalated: bool = False, error: str | None = None) -> None:
        self.escalated = escalated
        self.error = error
        self.stop_reasons: list[str | None] = []

    async def stop(self, *, reason: str | None = None, progress: Any = None) -> StopResult:
        _ = progress
        self.stop_reasons.append(reason)
        if self.error is not None:
            raise RuntimeError(self.error)
        return StopResult(escalated=self.escalated)


_OUTCOME = DrainOutcome(status="succeeded", exit_code=0)


@pytest.mark.asyncio
async def test_default_teardown_stops_connection_without_emitting_events() -> None:
    connection = _StopConnection()

    report = await DefaultDrainSessionTeardown().stop_connection(connection, _OUTCOME)  # type: ignore[arg-type]

    assert connection.stop_reasons == ["quiescent"]
    assert report.escalated is False
    assert report.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("escalated", "error", "expected_phases", "expected_statuses"),
    [
        (False, None, ["cleanup_running", "cleanup_completed"], ["running", "completed"]),
        (
            True,
            None,
            ["cleanup_running", "cleanup_escalated", "cleanup_completed"],
            ["running", "escalated", "escalated"],
        ),
        (False, "stop failed", ["cleanup_running", "cleanup_failed"], ["running", "failed"]),
    ],
)
async def test_pi_teardown_emits_exact_cleanup_phase_sequence(
    escalated: bool,
    error: str | None,
    expected_phases: list[str],
    expected_statuses: list[str],
) -> None:
    connection = _StopConnection(escalated=escalated, error=error)
    emitted: list[HarnessEvent] = []
    spawn_id = SpawnId("p-teardown")

    def emit_event(target_id: SpawnId, event: HarnessEvent) -> None:
        assert target_id == spawn_id
        emitted.append(event)

    teardown = PiDrainSessionTeardown(
        spawn_id=spawn_id,
        emit_event=emit_event,
    )

    report = await teardown.stop_connection(connection, _OUTCOME)  # type: ignore[arg-type]

    assert connection.stop_reasons == ["quiescent"]
    assert [event.payload["phase"] for event in emitted] == expected_phases
    assert [event.payload["cleanup_status"] for event in emitted] == expected_statuses
    assert all(event.event_type == "meridian.pi.lifecycle.phase" for event in emitted)
    assert all(event.harness_id == "pi" for event in emitted)
    assert all(event.payload["spawn_id"] == "p-teardown" for event in emitted)
    assert report.escalated is escalated
    assert report.error == error
    if escalated:
        assert emitted[1].payload["reason"] == "abort_grace_expired"
    if error is not None:
        assert emitted[-1].payload["error"] == error
