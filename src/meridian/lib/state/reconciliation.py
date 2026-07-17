"""Shared spawn reconciliation decisions."""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import resolve_completion_cancel_precedence
from meridian.lib.state.spawn.model import SpawnRecord


@dataclass(frozen=True)
class Skip:
    reason: str


@dataclass(frozen=True)
class FinalizeFailed:
    error: str
    exit_code: int = 1


@dataclass(frozen=True)
class FinalizeSucceededFromReport:
    pass


@dataclass(frozen=True)
class FinalizeFromRunnerExit:
    status: SpawnStatus
    exit_code: int
    error: str | None


type ReconciliationDecision = (
    Skip | FinalizeFailed | FinalizeSucceededFromReport | FinalizeFromRunnerExit
)


def completion_or_cancel_decision(
    record: SpawnRecord,
    durable_report_completion: bool,
) -> ReconciliationDecision | None:
    """Resolve durable completion against an outstanding cancel request."""

    intent = record.cancel_intent
    resolved = resolve_completion_cancel_precedence(
        durable_report_completion=durable_report_completion,
        cancel_requested=intent is not None,
        cancel_exit_code=intent.exit_code if intent is not None else 130,
        cancel_error=intent.error if intent is not None else "cancelled",
    )
    if resolved is None:
        return None
    if resolved.status == "succeeded":
        return FinalizeSucceededFromReport()
    return FinalizeFromRunnerExit(
        status=resolved.status,
        exit_code=resolved.exit_code,
        error=resolved.error,
    )
