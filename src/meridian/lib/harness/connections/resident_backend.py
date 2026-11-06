"""Explicit resident-backend control seam for long-lived harness connections."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from meridian.lib.harness.connections.liveness import BackendLivenessPolicy, LivenessDecision


class ResidentBackendControl(Protocol):
    """Control surface for resident-until-done backends.

    This is intentionally narrower than ``HarnessConnection``: the resident drain
    loop needs structured liveness and descendant-wait signaling without knowing
    transport internals or optional implementation fields. Nudges go through
    ``SpawnManager.deliver`` like every other user message.
    """

    def health_status(self) -> LivenessDecision:
        """Return structured backend liveness; do not collapse dead and stalled."""
        ...

    def set_awaiting_done(self, awaiting: bool) -> None:
        """Tell backend liveness that Meridian is intentionally awaiting child work."""
        ...


class LivenessResidentBackendControl:
    """Resident control backed by ``BackendLivenessPolicy`` and adapter callbacks."""

    def __init__(
        self,
        *,
        liveness: BackendLivenessPolicy,
        backend_dead: Callable[[], bool],
    ) -> None:
        self._liveness = liveness
        self._backend_dead = backend_dead

    def health_status(self) -> LivenessDecision:
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


__all__ = [
    "LivenessResidentBackendControl",
    "ResidentBackendControl",
]
