"""Claude's native background-task evidence for the shared resident drain."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from meridian.lib.streaming.completion_contracts import (
    EvidenceEventDecision,
    WorkAssessment,
)
from meridian.lib.utils.json_shapes import json_list

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import RawHarnessEvent
    from meridian.lib.streaming.completion_contracts import AssessmentTrigger, CompletionState


class ClaudeCompletionEvidence:
    """Hold through the empty snapshot until Claude supplies its follow-up result."""

    def __init__(self, *, poll_seconds: float) -> None:
        self._pending_tasks = False
        self._hold = False
        self._generation = 0
        self._poll_seconds = poll_seconds
        self._next_poll_at = time.monotonic() + poll_seconds

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def observe_event(
        self, event: RawHarnessEvent, transition: str | None
    ) -> EvidenceEventDecision:
        del transition
        if event.harness_id != "claude":
            return EvidenceEventDecision()
        if (
            event.event_type == "system"
            and event.payload.get("subtype") == "background_tasks_changed"
        ):
            tasks = json_list(event.payload.get("tasks"))
            if tasks is None:
                raise ValueError("Claude background_tasks_changed missing tasks array")
            self._pending_tasks = bool(tasks)
            self._hold = self._hold or self._pending_tasks
            self._generation += 1
        elif event.event_type == "result":
            self._hold = self._pending_tasks
            self._generation += 1
        return EvidenceEventDecision()

    def note_event_delivered(
        self, event: RawHarnessEvent, state: CompletionState
    ) -> EvidenceEventDecision:
        return EvidenceEventDecision()

    async def assess(self, trigger: AssessmentTrigger, state: CompletionState) -> WorkAssessment:
        return WorkAssessment(
            disposition="ready", blockers=(), generation=self._generation,
            completion_hold=self._hold,
        )

    def request_validation(self) -> int:
        return self._generation

    def validation_complete(self, request: int) -> bool:
        return request == self._generation

    def next_due_at(self) -> float:
        # A quiet native stream still needs to consume done/rearm signal files.
        return self._next_poll_at

    async def handle_due(self) -> EvidenceEventDecision:
        self._next_poll_at = time.monotonic() + self._poll_seconds
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        return False

    async def wait_for_change(self) -> None:
        raise RuntimeError("Claude completion evidence has no auxiliary wake source")
