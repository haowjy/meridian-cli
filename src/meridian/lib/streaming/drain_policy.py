from __future__ import annotations

from collections.abc import Callable
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


class PiRpcQuiescenceDrainPolicy:
    """Pi spawned-session policy: wait for quiescence before terminating."""

    def __init__(self, *, quiescence_check: Callable[[], bool]) -> None:
        self._quiescence_check = quiescence_check

    def classify(self, outcome: TerminalEventOutcome) -> DrainAction:
        if outcome.status != "succeeded":
            return DrainAction(terminate=True, emit_turn_boundary=False)
        if self._quiescence_check():
            return DrainAction(terminate=True, emit_turn_boundary=False)
        return DrainAction(terminate=False, emit_turn_boundary=True)


__all__ = [
    "TURN_BOUNDARY_EVENT_TYPE",
    "DrainAction",
    "DrainPolicy",
    "PersistentDrainPolicy",
    "PiRpcQuiescenceDrainPolicy",
    "SingleTurnDrainPolicy",
]
