"""Claude's native background-task evidence for the shared resident drain."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from meridian.lib.streaming.completion_contracts import (
    DiagnosticBlocker,
    EvidenceEventDecision,
    WorkAssessment,
)
from meridian.lib.utils.json_shapes import json_list, json_object

logger = structlog.get_logger()

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import RawHarnessEvent
    from meridian.lib.streaming.completion_contracts import AssessmentTrigger, CompletionState


class ClaudeCompletionEvidence:
    """Hold through the empty snapshot until Claude supplies its follow-up result."""

    def __init__(self) -> None:
        self._hold = False
        self._generation = 0
        self._task_blockers: tuple[DiagnosticBlocker, ...] = ()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def observe_event(
        self, event: RawHarnessEvent, transition: str | None
    ) -> EvidenceEventDecision:
        del transition
        if (
            event.event_type == "system"
            and event.payload.get("subtype") == "background_tasks_changed"
        ):
            tasks = json_list(event.payload.get("tasks"))
            if tasks is None:
                logger.warning(
                    "Claude background_tasks_changed missing tasks array; holding completion"
                )
                self._task_blockers = (DiagnosticBlocker(
                    source="native_task", code="running",
                    identity="Unknown background tasks (malformed snapshot)",
                ),)
            else:
                self._task_blockers = tuple(
                    DiagnosticBlocker(
                        source="native_task", code="running",
                        identity=(
                            f"{task.get('description', 'Unnamed task')} "
                            f"({task.get('task_type', 'unknown type')})"
                            f"{' (subagent)' if task.get('parent_task_id') else ''}"
                        ),
                    )
                    for raw in tasks
                    for task in [json_object(raw) or {}]
                )
            self._hold = self._hold or bool(self._task_blockers)
            self._generation += 1
        elif event.event_type == "result":
            self._hold = bool(self._task_blockers)
            self._generation += 1
        return EvidenceEventDecision()

    def note_event_delivered(
        self, event: RawHarnessEvent, state: CompletionState
    ) -> EvidenceEventDecision:
        return EvidenceEventDecision()

    async def assess(self, trigger: AssessmentTrigger, state: CompletionState) -> WorkAssessment:
        blockers = self._task_blockers
        if self._hold and not blockers:
            blockers = (DiagnosticBlocker(source="native_task", code="awaiting_followup_result"),)
        return WorkAssessment(
            disposition="blocked" if blockers else "ready",
            blockers=blockers,
            generation=self._generation,
        )

    def request_validation(self) -> int:
        return self._generation

    def validation_complete(self, request: int) -> bool:
        return True  # Native evidence updates synchronously as stream events arrive.

    def next_due_at(self) -> None:
        return None

    async def handle_due(self) -> EvidenceEventDecision:
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        return False

    async def wait_for_change(self) -> None:
        raise RuntimeError("Claude completion evidence has no auxiliary wake source")
