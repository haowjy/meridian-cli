"""Explicit resident-backend control seam for long-lived harness connections."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from meridian.lib.harness.connections.liveness import BackendLivenessPolicy, LivenessDecision

ResidentHealthStatus = LivenessDecision | Literal["unsupported"]
ResidentTurnState = Literal["idle", "active", "unknown"]


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

    def current_turn_state(self) -> ResidentTurnState:
        """Return whether the resident backend is idle enough for a follow-up turn."""
        ...


class LivenessResidentBackendControl:
    """Resident control backed by ``BackendLivenessPolicy`` and adapter callbacks."""

    def __init__(
        self,
        *,
        liveness: BackendLivenessPolicy,
        backend_dead: Callable[[], bool],
        begin_followup_turn: Callable[[str], Awaitable[None]],
        current_turn_state: Callable[[], ResidentTurnState] | None = None,
    ) -> None:
        self._liveness = liveness
        self._backend_dead = backend_dead
        self._begin_followup_turn = begin_followup_turn
        self._current_turn_state = current_turn_state

    def health_status(self) -> ResidentHealthStatus:
        try:
            decision = self._liveness.evaluate_stream_health()
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

    def current_turn_state(self) -> ResidentTurnState:
        if self._current_turn_state is None:
            return "unknown"
        return self._current_turn_state()


__all__ = [
    "LivenessResidentBackendControl",
    "ResidentBackendControl",
    "ResidentHealthStatus",
    "ResidentTurnState",
]
