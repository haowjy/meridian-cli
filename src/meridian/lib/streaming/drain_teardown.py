"""Harness-neutral connection teardown contract for completed drain sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection
    from meridian.lib.streaming.spawn_session import DrainOutcome


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


DEFAULT_DRAIN_SESSION_TEARDOWN = DefaultDrainSessionTeardown()


__all__ = [
    "DEFAULT_DRAIN_SESSION_TEARDOWN",
    "DefaultDrainSessionTeardown",
    "DrainSessionTeardown",
    "TeardownReport",
]
