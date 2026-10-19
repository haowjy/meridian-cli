"""Claude's native background-task source for the shared hold."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from meridian.lib.streaming.completion_contracts import DiagnosticBlocker
from meridian.lib.utils.json_shapes import json_list, json_object

logger = structlog.get_logger()

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import RawHarnessEvent

_AWAITING_FOLLOWUP_RESULT = (
    DiagnosticBlocker(source="native_task", code="awaiting_followup_result"),
)


class ClaudeNativeTaskSource:
    """Hold through the empty snapshot until Claude supplies its follow-up result."""

    def __init__(self) -> None:
        self._hold = False
        self._generation = 0
        self._task_blockers: tuple[DiagnosticBlocker, ...] = ()

    def observe_event(self, event: RawHarnessEvent) -> None:
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

    @property
    def blockers(self) -> tuple[DiagnosticBlocker, ...]:
        if self._hold and not self._task_blockers:
            return _AWAITING_FOLLOWUP_RESULT
        return self._task_blockers

    @property
    def generation(self) -> int:
        return self._generation
