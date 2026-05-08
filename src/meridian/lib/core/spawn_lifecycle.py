"""Shared spawn lifecycle decisions.

Durable completion evidence is authoritative. If Meridian later sends a cleanup
signal after a final report already exists, that cleanup must not downgrade the
spawn from succeeded to failed.
"""

import json
from dataclasses import dataclass
from typing import cast

from meridian.lib.core.domain import SpawnStatus

ACTIVE_SPAWN_STATUSES: frozenset[str] = frozenset({"queued", "running", "finalizing"})
TERMINAL_SPAWN_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "succeeded", "failed", "cancelled"}),
    "running": frozenset({"finalizing", "succeeded", "failed", "cancelled"}),
    "finalizing": frozenset({"succeeded", "failed", "cancelled"}),
}


@dataclass(frozen=True)
class ExecutionTerminalFacts:
    """Execution evidence reported by runners before lifecycle finalization."""

    exit_code: int
    failure_reason: str | None = None
    cancellation_observed: bool = False
    durable_report_completion: bool = False
    terminated_after_completion: bool = False


@dataclass(frozen=True)
class ExecutionTerminalOutcome:
    """Lifecycle-resolved terminal tuple derived from execution facts."""

    status: SpawnStatus
    exit_code: int
    error: str | None


def is_active_spawn_status(status: str) -> bool:
    return status in ACTIVE_SPAWN_STATUSES


def validate_transition(from_status: SpawnStatus, to_status: SpawnStatus) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise ValueError(f"Illegal spawn transition: {from_status} -> {to_status}")


def has_durable_report_completion(report_text: str | None) -> bool:
    """Return True when a non-empty final report is available on disk."""

    if not report_text or not report_text.strip():
        return False

    stripped = report_text.strip()
    try:
        payload_obj = json.loads(stripped)
    except json.JSONDecodeError:
        return True
    if not isinstance(payload_obj, dict):
        return True

    payload = cast("dict[str, object]", payload_obj)
    event_name = str(
        payload.get("event_type", payload.get("event", payload.get("type", "")))
    ).strip().lower()
    if event_name in {"cancelled", "error"}:
        return False

    nested = payload.get("payload")
    if isinstance(nested, dict):
        nested_payload = cast("dict[str, object]", nested)
        nested_name = str(
            nested_payload.get(
                "event_type",
                nested_payload.get("event", nested_payload.get("type", "")),
            )
        ).strip().lower()
        if nested_name in {"cancelled", "error"}:
            return False
    return True


def resolve_execution_terminal_state(
    *,
    exit_code: int,
    failure_reason: str | None,
    cancelled: bool = False,
    durable_report_completion: bool = False,
    terminated_after_completion: bool = False,
) -> tuple[SpawnStatus, int, str | None]:
    """Normalize one execution outcome into the persisted terminal state."""

    if durable_report_completion and terminated_after_completion:
        return "succeeded", 0, None
    if cancelled:
        resolved_exit_code = exit_code if exit_code != 0 else 130
        return "cancelled", resolved_exit_code, failure_reason
    if exit_code == 0:
        return "succeeded", 0, failure_reason
    return "failed", exit_code, failure_reason


def resolve_execution_terminal_outcome(
    facts: ExecutionTerminalFacts,
) -> ExecutionTerminalOutcome:
    """Resolve runner facts into the authoritative terminal tuple."""

    status, exit_code, error = resolve_execution_terminal_state(
        exit_code=facts.exit_code,
        failure_reason=facts.failure_reason,
        cancelled=facts.cancellation_observed,
        durable_report_completion=facts.durable_report_completion,
        terminated_after_completion=facts.terminated_after_completion,
    )
    return ExecutionTerminalOutcome(
        status=status,
        exit_code=exit_code,
        error=error,
    )


def resolve_reconciled_terminal_state(
    *,
    durable_report_completion: bool,
    fallback_error: str,
) -> tuple[SpawnStatus, int, str | None]:
    """Resolve the terminal state produced by read-path reconciliation."""

    if durable_report_completion:
        return "succeeded", 0, None
    return "failed", 1, fallback_error
