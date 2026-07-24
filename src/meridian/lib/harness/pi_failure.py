"""Pi failure extraction and output normalization."""

import json
import logging
from typing import cast

_PI_LIFECYCLE_PHASE_EVENT = "meridian.pi.lifecycle.phase"
_PI_LIFECYCLE_NOISE_PHASES = frozenset(
    {
        "cleanup_running",
        "cleanup_completed",
        "cleanup_escalated",
        "cleanup_failed",
        "process_spawned",
        "initial_prompt_sent",
        "waiting_for_first_pi_event_after_prompt",
        "first_pi_event_received",
        "first_pi_event_timeout",
        "first_pi_event_eof_before_response",
        "no_initial_prompt",
        "session_event_seen",
        "session_event_absent",
        "quiescence_micro_drain_started",
        "waiting_for_tracked_children",
    }
)


def _pi_failure_output_verbose() -> bool:
    """Whether Pi failure text should include JS stack traces (e.g. extension errors)."""
    return logging.getLogger().isEnabledFor(logging.INFO)


def _is_js_stack_trace_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("at "):
        return True
    if stripped.startswith("(") and "file:///" in stripped:
        return True
    return "file:///" in stripped and ("/index.js:" in stripped or ".ts:" in stripped)


def compact_pi_failure_output(message: str, *, verbose: bool | None = None) -> str:
    """Collapse Pi extension/JS stack noise for user-facing spawn failure output."""
    text = message.strip()
    if not text:
        return text
    show_verbose = _pi_failure_output_verbose() if verbose is None else verbose
    if show_verbose:
        return text

    lines = text.splitlines()
    has_stack = any(_is_js_stack_trace_line(line) for line in lines)
    first_line = lines[0].strip() if lines else ""
    is_extension_error = first_line.startswith("Extension ") and " error:" in first_line
    if not has_stack and not is_extension_error:
        return text

    compact: list[str] = []
    for line in lines:
        if _is_js_stack_trace_line(line):
            break
        stripped = line.strip()
        if stripped:
            compact.append(stripped)
    if not compact:
        return first_line or text
    return compact[0] if is_extension_error else "\n".join(compact)


def _event_name(payload: dict[str, object]) -> str:
    return (
        str(payload.get("event_type", payload.get("event", payload.get("type", ""))))
        .strip()
        .lower()
    )


def _failure_text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        parts = [_failure_text_from_value(item) for item in cast("list[object]", value)]
        return "\n".join(part for part in parts if part).strip()

    if isinstance(value, dict):
        payload = cast("dict[str, object]", value)
        parts: list[str] = []
        for key in ("text", "message", "output"):
            if key in payload:
                text = _failure_text_from_value(payload[key])
                if text:
                    parts.append(text)
        if "content" in payload:
            text = _failure_text_from_value(payload["content"])
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    return ""


def _history_record_payload(payload_obj: object) -> dict[str, object] | None:
    if not isinstance(payload_obj, dict):
        return None
    record = cast("dict[str, object]", payload_obj)
    if "event_type" in record or "payload" in record:
        return record
    return {"payload": record}


def _unwrap_history_payload(record: dict[str, object]) -> dict[str, object]:
    nested = record.get("payload")
    if isinstance(nested, dict):
        return cast("dict[str, object]", nested)
    return record


def is_pi_lifecycle_noise_payload(payload: dict[str, object]) -> bool:
    event_type = _event_name(payload)
    if event_type != _PI_LIFECYCLE_PHASE_EVENT:
        inner_type = str(payload.get("type", "")).strip().lower()
        if inner_type != _PI_LIFECYCLE_PHASE_EVENT:
            return False
    phase = str(payload.get("phase", "")).strip().lower()
    return phase in _PI_LIFECYCLE_NOISE_PHASES or (
        phase == "finalized" and not payload.get("error")
    )


def _pi_failure_from_payload(payload: dict[str, object]) -> str | None:
    event_type = _event_name(payload)
    if event_type == "response":
        command = str(payload.get("command", "")).strip().lower()
        is_inject_response = payload.get("meridian_control_action") == "inject"
        if command == "prompt" and payload.get("success") is False and not is_inject_response:
            error = _failure_text_from_value(payload.get("error"))
            return error or "pi_prompt_rejected"
    if event_type == _PI_LIFECYCLE_PHASE_EVENT:
        phase = str(payload.get("phase", "")).strip().lower()
        if phase == "finalized":
            error = _failure_text_from_value(payload.get("error"))
            if error:
                return error
    if event_type == "error":
        message = _failure_text_from_value(payload.get("message"))
        if message:
            return message
    nested = payload.get("payload")
    if isinstance(nested, dict):
        return _pi_failure_from_payload(cast("dict[str, object]", nested))
    return None


def extract_pi_failure_from_history(output_lines: str) -> str | None:
    """Extract the last Pi failure recorded in newline-delimited history."""
    last_failure: str | None = None
    for line in output_lines.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload_obj: object = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        record = _history_record_payload(payload_obj)
        if record is None:
            continue
        event_type = str(record.get("event_type", "")).strip().lower()
        payload = _unwrap_history_payload(record)
        if event_type and _event_name(payload) != event_type:
            merged = dict(payload)
            merged.setdefault("type", event_type)
            payload = merged
        failure = _pi_failure_from_payload(payload)
        if failure:
            last_failure = compact_pi_failure_output(failure)
    return last_failure
