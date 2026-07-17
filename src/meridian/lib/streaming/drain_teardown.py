"""Plan-owned connection teardown for completed drain sessions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.pi_lifecycle_events import build_pi_phase_event

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection, HarnessEvent
    from meridian.lib.streaming.spawn_session import DrainOutcome

EmitEvent = Callable[[SpawnId, "HarnessEvent"], None]


@dataclass(frozen=True)
class TeardownReport:
    """Connection-stop result retained by a teardown strategy."""

    escalated: bool = False
    error: str | None = None


class DrainSessionTeardown(Protocol):
    """Stop policy selected with the drain plan for one session."""

    async def stop_connection(
        self,
        receiver: HarnessConnection[Any],
        outcome: DrainOutcome,
    ) -> TeardownReport: ...


@dataclass(frozen=True)
class DefaultDrainSessionTeardown:
    """Plain connection stop used outside harness-specific teardown policy."""

    async def stop_connection(
        self,
        receiver: HarnessConnection[Any],
        outcome: DrainOutcome,
    ) -> TeardownReport:
        _ = outcome
        stop_result = await receiver.stop(reason="quiescent")
        return TeardownReport(escalated=stop_result.escalated)


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


DEFAULT_DRAIN_SESSION_TEARDOWN = DefaultDrainSessionTeardown()


__all__ = [
    "DEFAULT_DRAIN_SESSION_TEARDOWN",
    "DefaultDrainSessionTeardown",
    "DrainSessionTeardown",
    "EmitEvent",
    "PiDrainSessionTeardown",
    "TeardownReport",
]
