"""Pure terminal write policy for spawn finalization events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from meridian.lib.core.spawn_lifecycle import (
    TERMINAL_SPAWN_STATUSES as _TERMINAL_SPAWN_STATUSES,
)
from meridian.lib.state.spawn.model import AUTHORITATIVE_ORIGINS

TerminalWriteDisposition = Literal["append", "replace", "reject"]


@dataclass(frozen=True)
class TerminalWriteDecision:
    """Decision for an incoming terminal write."""

    disposition: TerminalWriteDisposition


def decide_terminal_write(
    current_status: str | None,
    current_terminal_origin: str | None,
    incoming_origin: str,
) -> TerminalWriteDecision:
    """Decide whether a finalize event should write terminal fields.

    Rejected events are true no-ops: the locked mutation preserves the current
    state and reports that no write occurred.
    """

    if current_status is None:
        return TerminalWriteDecision("reject")

    already_terminal = current_status in _TERMINAL_SPAWN_STATUSES
    if not already_terminal:
        return TerminalWriteDecision("append")

    incoming_authoritative = incoming_origin in AUTHORITATIVE_ORIGINS
    if current_terminal_origin == "reconciler" and incoming_authoritative:
        return TerminalWriteDecision("replace")

    return TerminalWriteDecision("reject")


__all__ = [
    "TerminalWriteDecision",
    "TerminalWriteDisposition",
    "decide_terminal_write",
]
