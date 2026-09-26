"""Pi failure extraction and output normalization."""

import logging
from typing import cast

from meridian.lib.harness.pi_lifecycle_events import PI_PHASE_EVENT_TYPE


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


def pi_failure_from_payload(payload: dict[str, object]) -> str | None:
    event_type = _event_name(payload)
    if event_type == "response":
        command = str(payload.get("command", "")).strip().lower()
        is_inject_response = payload.get("meridian_control_action") == "inject"
        if command == "prompt" and payload.get("success") is False and not is_inject_response:
            error = _failure_text_from_value(payload.get("error"))
            return error or "pi_prompt_rejected"
    if event_type == PI_PHASE_EVENT_TYPE:
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
        return pi_failure_from_payload(cast("dict[str, object]", nested))
    return None
