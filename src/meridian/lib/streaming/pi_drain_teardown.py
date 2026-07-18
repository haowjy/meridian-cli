"""Pi-specific connection teardown policy for completed drain sessions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.pi_lifecycle_events import build_pi_phase_event
from meridian.lib.streaming.drain_teardown import TeardownReport

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
    from meridian.lib.streaming.spawn_session import DrainOutcome

EmitEvent = Callable[[SpawnId, "RawHarnessEvent"], None]


@dataclass(frozen=True)
class PiDrainSessionTeardown:
    """Pi connection stop with lifecycle phase emission."""

    spawn_id: SpawnId
    emit_event: EmitEvent

    async def stop_connection(
        self,
        receiver: HarnessConnection[Any],
        outcome: DrainOutcome,
    ) -> TeardownReport:
        _ = outcome
        self._emit_phase(
            receiver,
            phase="cleanup_running",
            cleanup_status="running",
        )
        try:
            stop_result = await receiver.stop(reason="quiescent")
            cleanup_status = "escalated" if stop_result.escalated else "completed"
            if stop_result.escalated:
                self._emit_phase(
                    receiver,
                    phase="cleanup_escalated",
                    cleanup_status="escalated",
                    reason="abort_grace_expired",
                )
            self._emit_phase(
                receiver,
                phase="cleanup_completed",
                cleanup_status=cleanup_status,
            )
            return TeardownReport(escalated=stop_result.escalated)
        except Exception as exc:
            self._emit_phase(
                receiver,
                phase="cleanup_failed",
                cleanup_status="failed",
                error=str(exc),
            )
            return TeardownReport(error=str(exc))

    def _emit_phase(
        self,
        receiver: HarnessConnection[Any],
        *,
        phase: str,
        **data: object,
    ) -> None:
        self.emit_event(
            self.spawn_id,
            build_pi_phase_event(self.spawn_id, receiver, phase, **data),
        )


__all__ = ["EmitEvent", "PiDrainSessionTeardown"]
