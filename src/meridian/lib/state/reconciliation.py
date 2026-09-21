"""Shared spawn reconciliation decisions."""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.core.domain import TerminalSpawnStatus
from meridian.lib.core.spawn_lifecycle import resolve_completion_cancel_precedence
from meridian.lib.state.spawn.model import SpawnRecord


@dataclass(frozen=True)
class Skip:
    reason: str


@dataclass(frozen=True)
class FinalizeFailed:
    error: str
    exit_code: int = 1
    #: Set when the terminal record still carries managed-primary fallback
    #: scopes (backend/TUI) that a later release must tear down.
    managed_scopes_pending: bool = False


@dataclass(frozen=True)
class FinalizeSucceededFromReport:
    pass


@dataclass(frozen=True)
class FinalizeFromRunnerExit:
    status: TerminalSpawnStatus
    exit_code: int
    error: str | None
    #: Set when the reconciler must also clean up managed-primary fallback
    #: scopes (backend/TUI) derived from the primary metadata.
    managed_scopes_pending: bool = False


type ReconciliationDecision = (
    Skip | FinalizeFailed | FinalizeSucceededFromReport | FinalizeFromRunnerExit
)


def completion_or_cancel_decision(
    record: SpawnRecord,
    durable_report_completion: bool,
) -> ReconciliationDecision | None:
    """Resolve durable completion against an outstanding cancel request.

    Runner-exit evidence is threaded through so a cancelled or abnormal exit
    outranks recovered report text, matching the runner finalization path.
    """

    intent = record.cancel_intent
    runner_exit = record.runner_exit
    resolved = resolve_completion_cancel_precedence(
        durable_report_completion=durable_report_completion,
        cancel_requested=intent is not None,
        cancel_exit_code=intent.exit_code if intent is not None else 130,
        cancel_error=intent.error if intent is not None else "cancelled",
        execution_exit_code=(
            runner_exit.exit_code if runner_exit is not None else record.last_attempt_exit_code
        ),
        execution_terminal_status=runner_exit.status if runner_exit is not None else None,
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
