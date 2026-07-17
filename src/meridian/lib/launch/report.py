"""Spawn report extraction from assistant output with report.md preference."""

import json
import logging
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.spawn_lifecycle import (
    DurableReportEvidence,
    classify_durable_report_text,
    is_control_report_payload,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import SpawnExtractor
from meridian.lib.launch.constants import HISTORY_FILENAME, OUTPUT_FILENAME
from meridian.lib.state.artifact_store import ArtifactStore

from .artifact_io import read_artifact_text

ReportSource = Literal["report_md", "assistant_message", "failure_reason", "pi_failure"]
_LOGGER = logging.getLogger(__name__)
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
        "waiting_for_notification_completion",
    }
)


class ExtractedReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str | None
    source: ReportSource | None


def _event_name(payload: dict[str, object]) -> str:
    return (
        str(payload.get("event_type", payload.get("event", payload.get("type", ""))))
        .strip()
        .lower()
    )


def _is_terminal_control_frame(text: str) -> bool:
    return classify_durable_report_text(text) is DurableReportEvidence.CONTROL_FRAME


def _text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        parts = [_text_from_value(item) for item in cast("list[object]", value)]
        return "\n".join(part for part in parts if part).strip()

    if isinstance(value, dict):
        payload = cast("dict[str, object]", value)
        parts: list[str] = []
        for key in ("text", "message", "output"):
            if key in payload:
                text = _text_from_value(payload[key])
                if text:
                    parts.append(text)
        if "content" in payload:
            text = _text_from_value(payload["content"])
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    return ""


def _assistant_texts(payload: object) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        obj = cast("dict[str, object]", payload)
        role = str(obj.get("role", "")).lower()
        event_type = str(obj.get("type", obj.get("event", ""))).lower()

        if role == "assistant" or "assistant" in event_type:
            content_text = _text_from_value(obj.get("content"))
            if content_text:
                found.append(content_text)
            for key in ("text", "message", "output"):
                text = _text_from_value(obj.get(key))
                if text:
                    found.append(text)

        if "choices" in obj and isinstance(obj["choices"], list):
            for choice in cast("list[object]", obj["choices"]):
                if not isinstance(choice, dict):
                    continue
                choice_payload = cast("dict[str, object]", choice)
                message = choice_payload.get("message")
                if isinstance(message, dict):
                    message_payload = cast("dict[str, object]", message)
                    message_role = str(message_payload.get("role", "")).lower()
                    if message_role == "assistant":
                        text = _text_from_value(message_payload.get("content"))
                        if text:
                            found.append(text)

        for nested in obj.values():
            found.extend(_assistant_texts(nested))
        return found

    if isinstance(payload, list):
        for item in cast("list[object]", payload):
            found.extend(_assistant_texts(item))
    return found


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


def _is_pi_lifecycle_noise_payload(payload: dict[str, object]) -> bool:
    event_type = _event_name(payload)
    if event_type != _PI_LIFECYCLE_PHASE_EVENT:
        inner_type = str(payload.get("type", "")).strip().lower()
        if inner_type != _PI_LIFECYCLE_PHASE_EVENT:
            return False
    phase = str(payload.get("phase", "")).strip().lower()
    return phase in _PI_LIFECYCLE_NOISE_PHASES or (
        phase == "finalized" and not payload.get("error")
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


def _pi_failure_from_payload(payload: dict[str, object]) -> str | None:
    event_type = _event_name(payload)
    if event_type == "response":
        command = str(payload.get("command", "")).strip().lower()
        is_inject_response = payload.get("meridian_control_action") == "inject"
        if command == "prompt" and payload.get("success") is False and not is_inject_response:
            error = _text_from_value(payload.get("error"))
            return error or "pi_prompt_rejected"
    if event_type == _PI_LIFECYCLE_PHASE_EVENT:
        phase = str(payload.get("phase", "")).strip().lower()
        if phase == "finalized":
            error = _text_from_value(payload.get("error"))
            if error:
                return error
    if event_type == "error":
        message = _text_from_value(payload.get("message"))
        if message:
            return message
    nested = payload.get("payload")
    if isinstance(nested, dict):
        return _pi_failure_from_payload(cast("dict[str, object]", nested))
    return None


def extract_pi_failure_from_history(output_lines: str) -> str | None:
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


def _extract_last_assistant_message(output_lines: str) -> str | None:
    last_assistant: str | None = None
    last_text_line: str | None = None
    for line in output_lines.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload_obj: object = json.loads(stripped)
        except json.JSONDecodeError:
            last_text_line = stripped
            continue
        record = _history_record_payload(payload_obj)
        if record is not None:
            payload = _unwrap_history_payload(record)
            if _is_pi_lifecycle_noise_payload(payload):
                continue
            if is_control_report_payload(payload):
                continue
        elif isinstance(payload_obj, dict):
            if is_control_report_payload(cast("dict[str, object]", payload_obj)):
                continue
        assistants = _assistant_texts(cast("object", payload_obj))
        if assistants:
            last_assistant = assistants[-1].strip()
            continue
        last_text_line = stripped
    if last_assistant:
        return last_assistant
    return last_text_line


def _normalized_history_lines(raw_lines: str) -> str:
    normalized: list[str] = []
    for line in raw_lines.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload_obj: object = json.loads(stripped)
        except json.JSONDecodeError:
            normalized.append(stripped)
            continue
        if isinstance(payload_obj, dict) and "seq" in payload_obj and "payload" in payload_obj:
            payload = cast("dict[str, object]", payload_obj)
            payload_obj = payload["payload"]
            event_type = str(payload.get("event_type", "")).strip()
            if event_type and isinstance(payload_obj, dict):
                normalized_payload = dict(cast("dict[str, object]", payload_obj))
                normalized_payload.setdefault("type", event_type)
                payload_obj = normalized_payload
        normalized.append(json.dumps(payload_obj, separators=(",", ":"), sort_keys=True))
    return "\n".join(normalized)


def _synthesized_failure_report(failure_reason: str) -> str:
    return failure_reason.strip()


def extract_or_fallback_report(
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    *,
    extractor: SpawnExtractor | None = None,
    failure_reason: str | None = None,
) -> ExtractedReport:
    """Extract report text from assistant output, preferring report.md when available."""

    report_content = read_artifact_text(artifacts, spawn_id, "report.md").strip()
    if report_content and not _is_terminal_control_frame(report_content):
        return ExtractedReport(content=report_content, source="report_md")

    if extractor is not None:
        try:
            adapted_report = extractor.extract_report(artifacts, spawn_id)
        except Exception:
            _LOGGER.warning(
                "extractor.extract_report failed for spawn %s",
                spawn_id,
                exc_info=True,
            )
        else:
            adapted_text = adapted_report.strip() if adapted_report else ""
            if adapted_text and not _is_terminal_control_frame(adapted_text):
                history_text = read_artifact_text(artifacts, spawn_id, HISTORY_FILENAME).strip()
                pi_failure = (
                    extract_pi_failure_from_history(_normalized_history_lines(history_text))
                    if history_text
                    else None
                )
                if pi_failure and pi_failure.strip() == adapted_text:
                    return ExtractedReport(content=adapted_text, source="pi_failure")
                return ExtractedReport(content=adapted_text, source="assistant_message")

    output_lines = read_artifact_text(artifacts, spawn_id, HISTORY_FILENAME).strip()
    if not output_lines:
        output_lines = read_artifact_text(artifacts, spawn_id, OUTPUT_FILENAME)
    else:
        output_lines = _normalized_history_lines(output_lines)

    if output_lines.strip():
        pi_failure = extract_pi_failure_from_history(output_lines)
        if pi_failure and not _is_terminal_control_frame(pi_failure):
            return ExtractedReport(content=pi_failure, source="pi_failure")

    assistant_message = _extract_last_assistant_message(output_lines)
    assistant_report = assistant_message.strip() if assistant_message else ""
    if assistant_report and not _is_terminal_control_frame(assistant_report):
        return ExtractedReport(content=assistant_report, source="assistant_message")

    if failure_reason and failure_reason.strip():
        synthesized = _synthesized_failure_report(failure_reason)
        return ExtractedReport(content=synthesized, source="failure_reason")

    return ExtractedReport(content=None, source=None)
