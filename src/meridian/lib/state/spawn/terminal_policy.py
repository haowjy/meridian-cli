"""Pure terminal write policy for spawn finalization events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from meridian.lib.core.spawn_lifecycle import (
    TERMINAL_SPAWN_STATUSES as _TERMINAL_SPAWN_STATUSES,
)

TerminalWriteDisposition = Literal["append", "replace", "reject"]


@dataclass(frozen=True)
class TerminalWriteDecision:
    """Decision for an incoming terminal write."""

    disposition: TerminalWriteDisposition


def _authoritative_origins() -> frozenset[str]:
    # Runtime import avoids a cycle: spawn_store owns the event-origin constants
    # and imports the event reducer during module initialization.
    from meridian.lib.state.spawn_store import AUTHORITATIVE_ORIGINS

    return AUTHORITATIVE_ORIGINS


def decide_terminal_write(
    current_status: str | None,
    current_terminal_origin: str | None,
    incoming_origin: str,
) -> TerminalWriteDecision:
    """Decide whether a finalize event should write terminal fields.

    This reproduces the reducer's pre-policy decision lattice. Rejected events
    are still partially merged by the reducer until the Phase 1.3 hardening
    changes reject into a true no-op.
    """

    if current_status is None:
        return TerminalWriteDecision("reject")

    already_terminal = current_status in _TERMINAL_SPAWN_STATUSES
    if not already_terminal:
        return TerminalWriteDecision("append")

    incoming_authoritative = incoming_origin in _authoritative_origins()
    if current_terminal_origin == "reconciler" and incoming_authoritative:
        return TerminalWriteDecision("replace")

    return TerminalWriteDecision("reject")


__all__ = [
    "TerminalWriteDecision",
    "TerminalWriteDisposition",
    "decide_terminal_write",
]
