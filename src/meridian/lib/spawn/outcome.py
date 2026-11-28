"""A launch's terminal outcome from its execution facts.

Durable completion evidence belongs to the live runner's execution resolution and
the lockless compatibility path. A dead lock-backed launch derives its outcome from
the free launch lock and cancel intent, not from its report.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.core.domain import TerminalSpawnStatus


@dataclass(frozen=True)
class ExecutionTerminalFacts:
    exit_code: int
    failure_reason: str | None = None
    cancellation_observed: bool = False
    durable_report_completion: bool = False
    terminal_status: TerminalSpawnStatus | None = None


@dataclass(frozen=True)
class ExecutionTerminalOutcome:
    status: TerminalSpawnStatus
    exit_code: int
    error: str | None


def resolve_execution_terminal_state(
    *,
    exit_code: int,
    failure_reason: str | None,
    cancelled: bool = False,
    durable_report_completion: bool = False,
    terminal_status: TerminalSpawnStatus | None = None,
) -> tuple[TerminalSpawnStatus, int, str | None]:
    """Normalize one execution outcome into the persisted terminal state.

    Execution facts outrank report text: a spawn that was cancelled or exited
    non-zero is not a success merely because extraction recovered prose. An
    explicit non-succeeded ``terminal_status`` (e.g. timeout) remains
    authoritative. Durable report completion is only consulted once execution
    is consistent with success.
    """

    if terminal_status is not None and terminal_status != "succeeded":
        return terminal_status, exit_code, failure_reason

    # A cancelled attempt with an abnormal exit is cancelled regardless of any
    # extracted report text. A clean exit after a late cleanup signal keeps a
    # genuine report intact (see the module docstring).
    if cancelled and exit_code != 0:
        return "cancelled", exit_code, failure_reason

    # A non-zero exit without an explicit success verdict cannot be report success.
    if exit_code != 0 and terminal_status is None:
        return "failed", exit_code, failure_reason

    if durable_report_completion:
        return "succeeded", 0, None
    if terminal_status is not None:
        return terminal_status, exit_code, failure_reason
    if cancelled:
        resolved_exit_code = exit_code if exit_code != 0 else 130
        return "cancelled", resolved_exit_code, failure_reason
    if exit_code == 0:
        return "succeeded", 0, failure_reason
    return "failed", exit_code, failure_reason


def resolve_execution_terminal_outcome(
    facts: ExecutionTerminalFacts,
) -> ExecutionTerminalOutcome:

    status, exit_code, error = resolve_execution_terminal_state(
        exit_code=facts.exit_code,
        failure_reason=facts.failure_reason,
        cancelled=facts.cancellation_observed,
        durable_report_completion=facts.durable_report_completion,
        terminal_status=facts.terminal_status,
    )
    return ExecutionTerminalOutcome(
        status=status,
        exit_code=exit_code,
        error=error,
    )
