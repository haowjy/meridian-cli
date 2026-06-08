"""Explicit resident-backend control seam for long-lived harness connections."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from meridian.lib.harness.connections.liveness import BackendLivenessPolicy, LivenessDecision

ResidentHealthStatus = LivenessDecision | Literal["unsupported"]


class ResidentBackendControl(Protocol):
    """Control surface for resident-until-done backends.

    This is intentionally narrower than ``HarnessConnection``: the resident drain
    loop needs structured liveness, descendant-wait signaling, and idle follow-up
    turns without knowing transport internals or optional implementation fields.
    """

    def health_status(self) -> ResidentHealthStatus:
        """Return structured backend liveness; do not collapse dead and stalled."""
        ...

    def set_awaiting_done(self, awaiting: bool) -> None:
        """Tell backend liveness that Meridian is intentionally awaiting child work."""
        ...

    async def begin_followup_turn(self, message: str) -> None:
        """Start a new user turn on an already-resident idle backend."""
        ...


class LivenessResidentBackendControl:
    """Resident control backed by ``BackendLivenessPolicy`` and adapter callbacks."""

    def __init__(
        self,
        *,
        liveness: BackendLivenessPolicy,
        backend_dead: Callable[[], bool],
        begin_followup_turn: Callable[[str], Awaitable[None]],
    ) -> None:
        self._liveness = liveness
        self._backend_dead = backend_dead
        self._begin_followup_turn = begin_followup_turn

    def health_status(self) -> ResidentHealthStatus:
        try:
            decision = self._liveness.classify_close_stream()
        except Exception:
            return LivenessDecision.BACKEND_DEAD
        if decision == LivenessDecision.BACKEND_DEAD:
            return decision
        try:
            if self._backend_dead():
                return LivenessDecision.BACKEND_DEAD
        except Exception:
            return LivenessDecision.BACKEND_DEAD
        return decision

    def set_awaiting_done(self, awaiting: bool) -> None:
        self._liveness.set_awaiting_done(awaiting)

    async def begin_followup_turn(self, message: str) -> None:
        await self._begin_followup_turn(message)


__all__ = [
    "LivenessResidentBackendControl",
    "ResidentBackendControl",
    "ResidentHealthStatus",
]
