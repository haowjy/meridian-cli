"""Explicit resident-backend control seam for long-lived harness connections."""

from __future__ import annotations

from typing import Protocol


class ResidentBackendControl(Protocol):
    """Control surface for resident-until-done backends.

    This is intentionally narrower than ``HarnessConnection``. Liveness is
    ``HarnessConnection.backend_dead``, and nudges go through
    ``SpawnManager.deliver`` like every other user message.
    """

    def set_awaiting_done(self, awaiting_done: bool) -> None:
        """Tell backend liveness that Meridian is intentionally awaiting child work."""
        ...


__all__ = ["ResidentBackendControl"]
