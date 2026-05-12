from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from meridian.lib.harness.semantics import TerminalEventOutcome

TURN_BOUNDARY_EVENT_TYPE = "meridian/turn_completed"


@dataclass(frozen=True)
class DrainAction:
    """Result of policy classification for a terminal event."""

    terminate: bool  # True -> break the drain loop
    emit_turn_boundary: bool  # True -> fan out synthetic turn_completed event


class DrainPolicy(Protocol):
    """Strategy for classifying terminal events in drain loops."""

    def classify(self, outcome: TerminalEventOutcome) -> DrainAction: ...


class SingleTurnDrainPolicy:
    """Default: first terminal event ends the session."""

    def classify(self, outcome: TerminalEventOutcome) -> DrainAction:
        return DrainAction(terminate=True, emit_turn_boundary=False)


class PersistentDrainPolicy:
    """Persistent: succeeded turns emit a boundary and continue; errors/cancels terminate."""

    def classify(self, outcome: TerminalEventOutcome) -> DrainAction:
        if outcome.status == "succeeded":
            return DrainAction(terminate=False, emit_turn_boundary=True)
        return DrainAction(terminate=True, emit_turn_boundary=False)


__all__ = [
    "TURN_BOUNDARY_EVENT_TYPE",
    "DrainAction",
    "DrainPolicy",
    "PersistentDrainPolicy",
    "SingleTurnDrainPolicy",
]
