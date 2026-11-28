"""A spawn's ``report.md``: reading it, and whether its text is completion evidence."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import cast

from meridian.lib.state.paths import LaunchPaths

_CONTROL_EVENT_NAMES: frozenset[str] = frozenset({"cancelled", "error", "error.connectionclosed"})
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
# Structured payloads must carry explicit report text to count as completion;
# a harness event envelope (e.g. ``permission.asked``) carries none.
_REPORT_TEXT_KEYS: tuple[str, ...] = ("result", "message", "output", "text", "content")


class DurableReportEvidence(StrEnum):
    ABSENT = "absent"
    COMPLETION = "completion"
    SYNTHETIC_FAILURE = "synthetic_failure"
    CONTROL_FRAME = "control_frame"


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
        event_type.startswith(prefix) for prefix in _OPENCODE_CONTROL_EVENT_NAMESPACE_PREFIXES
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
    return any(event_name.startswith(prefix) for prefix in _CODEX_CONTROL_EVENT_NAMESPACE_PREFIXES)


def is_control_report_payload(payload: dict[str, object]) -> bool:

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


def _has_report_text(payload: dict[str, object]) -> bool:

    for key in _REPORT_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
    nested = payload.get("payload")
    if isinstance(nested, dict):
        return _has_report_text(cast("dict[str, object]", nested))
    return False


def classify_durable_report_text(report_text: str | None) -> DurableReportEvidence:
    """Classify report text as lifecycle completion evidence.

    Completion requires a positive signal: non-JSON report text, or a structured
    payload that carries explicit report text. A raw harness event envelope (for
    example a ``permission.asked`` payload) is not completion evidence, even
    though it is non-empty JSON.
    """

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
    if _has_report_text(payload):
        return DurableReportEvidence.COMPLETION
    return DurableReportEvidence.ABSENT


def has_durable_report_completion(report_text: str | None) -> bool:

    return classify_durable_report_text(report_text) is DurableReportEvidence.COMPLETION


def read_spawn_report_text(launch: LaunchPaths) -> str | None:

    try:
        return launch.report.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def spawn_report_has_durable_completion(launch: LaunchPaths) -> bool:

    return has_durable_report_completion(read_spawn_report_text(launch))
