"""Shared spawn lifecycle decisions.

Durable completion evidence is authoritative. If Meridian later sends a cleanup
signal after a final report already exists, that cleanup must not downgrade the
spawn from succeeded to failed.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from meridian.lib.core.domain import SpawnStatus

# TODO(status-authority): SpawnStatus Literal in core/domain.py duplicates these sets.
ALL_SPAWN_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "finalizing", "succeeded", "failed", "cancelled", "timed_out"}
)
ACTIVE_SPAWN_STATUSES: frozenset[str] = frozenset({"queued", "running", "finalizing"})
TERMINAL_SPAWN_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out"}
)
FAILURE_SPAWN_STATUSES: frozenset[str] = frozenset({"failed", "timed_out"})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "succeeded", "failed", "cancelled", "timed_out"}),
    "running": frozenset({"finalizing", "succeeded", "failed", "cancelled", "timed_out"}),
    "finalizing": frozenset({"succeeded", "failed", "cancelled", "timed_out"}),
}
_CONTROL_EVENT_NAMES: frozenset[str] = frozenset(
    {"cancelled", "error", "error.connectionclosed"}
)
_OPENCODE_CONTROL_EVENT_TYPES = frozenset(
    {
        "sync",
    }
)
_OPENCODE_CONTROL_EVENT_NAMESPACE_PREFIXES: tuple[str, ...] = (
    "message.",
    "server.",
    "session.",
)
_CODEX_CONTROL_EVENT_NAMESPACE_PREFIXES: tuple[str, ...] = (
    "account.",
    "item.",
    "mcpserver.",
    "remotecontrol.",
    "thread.",
    "turn.",
)
_CLAUDE_PROGRESS_EVENT_NAMES: frozenset[str] = frozenset({"assistant", "user", "system"})
_CLAUDE_ENVELOPE_MARKER_KEYS: frozenset[str] = frozenset(
    {
        "message",
        "model",
        "parent_tool_use_id",
        "rate_limit_info",
        "request_id",
        "session_id",
        "usage",
        "uuid",
    }
)


class DurableReportEvidence(StrEnum):
    """Classification of persisted report text as lifecycle evidence."""

    ABSENT = "absent"
    COMPLETION = "completion"
    SYNTHETIC_FAILURE = "synthetic_failure"
    CONTROL_FRAME = "control_frame"


@dataclass(frozen=True)
class ExecutionTerminalFacts:
    """Execution evidence reported by runners before lifecycle finalization."""

    exit_code: int
    failure_reason: str | None = None
    cancellation_observed: bool = False
    durable_report_completion: bool = False


@dataclass(frozen=True)
class ExecutionTerminalOutcome:
    """Lifecycle-resolved terminal tuple derived from execution facts."""

    status: SpawnStatus
    exit_code: int
    error: str | None


def _event_name(payload: dict[str, object]) -> str:
    return (
        str(payload.get("event_type", payload.get("event", payload.get("type", ""))))
        .strip()
        .lower()
        .replace("/", ".")
    )


def _is_opencode_control_payload(payload: dict[str, object]) -> bool:
    event_type = _event_name(payload)
    return event_type in _OPENCODE_CONTROL_EVENT_TYPES or any(
        event_type.startswith(prefix)
        for prefix in _OPENCODE_CONTROL_EVENT_NAMESPACE_PREFIXES
    )


def _is_claude_result_payload(payload: dict[str, object]) -> bool:
    return _event_name(payload) == "result"


def _is_claude_completed_result_payload(payload: dict[str, object]) -> bool:
    return _is_claude_result_payload(payload) and payload.get("is_error") is False


def _is_claude_structured_payload(payload: dict[str, object]) -> bool:
    event_name = _event_name(payload)
    if event_name in _CLAUDE_PROGRESS_EVENT_NAMES or event_name == "result":
        return True
    return bool(event_name and any(key in payload for key in _CLAUDE_ENVELOPE_MARKER_KEYS))


def _is_claude_non_durable_payload(payload: dict[str, object]) -> bool:
    return _is_claude_structured_payload(payload) and not _is_claude_completed_result_payload(
        payload
    )


def _is_codex_control_event_payload(payload: dict[str, object]) -> bool:
    event_name = _event_name(payload)
    return any(
        event_name.startswith(prefix) for prefix in _CODEX_CONTROL_EVENT_NAMESPACE_PREFIXES
    )


def is_control_report_payload(payload: dict[str, object]) -> bool:
    """Return whether a structured payload is non-durable report/control evidence."""

    event_name = _event_name(payload)
    if event_name in _CONTROL_EVENT_NAMES:
        return True
    if event_name == "system":
        return True
    if _is_opencode_control_payload(payload):
        return True
    if _is_claude_non_durable_payload(payload):
        return True
    if _is_codex_control_event_payload(payload):
        return True

    nested = payload.get("payload")
    if isinstance(nested, dict):
        return is_control_report_payload(cast("dict[str, object]", nested))
    return False


def is_active_spawn_status(status: str) -> bool:
    return status in ACTIVE_SPAWN_STATUSES


def is_terminal_spawn_status(status: str) -> bool:
    return status in TERMINAL_SPAWN_STATUSES


def coerce_spawn_status(status: str) -> SpawnStatus:
    """Preserve every known spawn status; recover unknown persisted values as failed."""

    if status in ALL_SPAWN_STATUSES:
        return cast("SpawnStatus", status)
    return "failed"


def validate_transition(from_status: SpawnStatus, to_status: SpawnStatus) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise ValueError(f"Illegal spawn transition: {from_status} -> {to_status}")


def classify_durable_report_text(report_text: str | None) -> DurableReportEvidence:
    """Classify report text as lifecycle completion evidence."""

    if not report_text or not report_text.strip():
        return DurableReportEvidence.ABSENT

    stripped = report_text.strip()
    if stripped.lower().startswith("# report"):
        _, _, body = stripped.partition("\n")
        body = body.strip()
        if body:
            body_evidence = classify_durable_report_text(body)
            if body_evidence is not DurableReportEvidence.COMPLETION:
                return body_evidence
    if stripped.lower().startswith("# spawn failed"):
        return DurableReportEvidence.SYNTHETIC_FAILURE

    try:
        payload_obj = json.loads(stripped)
    except json.JSONDecodeError:
        return DurableReportEvidence.COMPLETION
    if not isinstance(payload_obj, dict):
        return DurableReportEvidence.COMPLETION

    payload = cast("dict[str, object]", payload_obj)
    if is_control_report_payload(payload):
        return DurableReportEvidence.CONTROL_FRAME
    return DurableReportEvidence.COMPLETION


def has_durable_report_completion(report_text: str | None) -> bool:
    """Return True when report text is durable completion evidence."""

    return classify_durable_report_text(report_text) is DurableReportEvidence.COMPLETION


def resolve_execution_terminal_state(
    *,
    exit_code: int,
    failure_reason: str | None,
    cancelled: bool = False,
    durable_report_completion: bool = False,
) -> tuple[SpawnStatus, int, str | None]:
    """Normalize one execution outcome into the persisted terminal state."""

    if durable_report_completion:
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
    )
    return ExecutionTerminalOutcome(
        status=status,
        exit_code=exit_code,
        error=error,
    )


def resolve_completion_cancel_precedence(
    *,
    durable_report_completion: bool,
    cancel_requested: bool,
    cancel_exit_code: int = 130,
    cancel_error: str | None = "cancelled",
) -> ExecutionTerminalOutcome | None:
    """Resolve the shared durable-completion-vs-late-cancel precedence rule."""

    if durable_report_completion:
        return ExecutionTerminalOutcome(status="succeeded", exit_code=0, error=None)
    if cancel_requested:
        return ExecutionTerminalOutcome(
            status="cancelled",
            exit_code=cancel_exit_code,
            error=cancel_error,
        )
    return None


def resolve_reconciled_terminal_state(
    *,
    durable_report_completion: bool,
    fallback_error: str,
) -> tuple[SpawnStatus, int, str | None]:
    """Resolve the terminal state produced by read-path reconciliation."""

    if durable_report_completion:
        return "succeeded", 0, None
    return "failed", 1, fallback_error
