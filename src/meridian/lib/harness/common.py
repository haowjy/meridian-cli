"""Shared helpers for harness adapters."""

import json
import re
from typing import cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.adapter import ArtifactStore, StreamEvent
from meridian.lib.launch.constants import HISTORY_FILENAME, OUTPUT_FILENAME

# ---------------------------------------------------------------------------
# Shared helpers (from _common.py)
# ---------------------------------------------------------------------------


def _payload_text(payload: dict[str, object], key: str, *, default: str = "?") -> str:
    value = payload.get(key)
    if value is None:
        return default
    rendered = str(value).strip()
    return rendered or default


def _synthesize_meridian_protocol_text(
    *,
    event_type: str,
    payload: dict[str, object],
) -> str | None:
    if event_type == "spawn.start":
        spawn_id = _payload_text(payload, "id")
        model = _payload_text(payload, "model")
        agent = payload.get("agent")
        if agent is None or not str(agent).strip():
            return f"{spawn_id} {model} started"
        return f"{spawn_id} {model} ({str(agent).strip()}) started"

    if event_type == "spawn.done":
        spawn_id = _payload_text(payload, "id")
        secs = _payload_text(payload, "secs")
        exit_code = _payload_text(payload, "exit")
        rendered = f"{spawn_id} completed {secs}s exit={exit_code}"
        tokens = payload.get("tok")
        if tokens is not None:
            rendered = f"{rendered} tok={tokens}"
        return rendered

    return None


def parse_json_stream_event(line: str) -> StreamEvent | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload_obj = json.loads(stripped)
    except json.JSONDecodeError:
        return StreamEvent(
            event_type="line",
            category="progress",
            raw_line=line,
            text=stripped,
        )

    if not isinstance(payload_obj, dict):
        return StreamEvent(
            event_type="line",
            category="progress",
            raw_line=line,
            text=stripped,
        )

    payload = cast("dict[str, object]", payload_obj)
    event_type = str(payload.get("type") or payload.get("t") or payload.get("event") or "line")
    text = payload.get("text") or payload.get("message")
    # Recognize both "spawn.*" and "meridian.spawn.*" as meridian protocol events.
    synth_type = event_type
    if synth_type.startswith("meridian."):
        synth_type = synth_type[len("meridian.") :]
    category = "sub-run" if synth_type.startswith("spawn.") else "progress"
    if text is None and "t" in payload and synth_type.startswith("spawn."):
        text = _synthesize_meridian_protocol_text(event_type=synth_type, payload=payload)
    if text is not None:
        return StreamEvent(
            event_type=event_type,
            category=category,
            raw_line=line,
            text=str(text),
            metadata=payload,
        )
    return StreamEvent(
        event_type=event_type,
        category=category,
        raw_line=line,
        text=None,
        metadata=payload,
    )


def categorize_stream_event(
    event: StreamEvent,
    *,
    exact_map: dict[str, str] | None = None,
) -> StreamEvent:
    normalized = event.event_type.strip().lower()
    category = _category_from_event_type(normalized, exact_map=exact_map)
    return StreamEvent(
        event_type=event.event_type,
        category=category,
        raw_line=event.raw_line,
        text=event.text,
        metadata=event.metadata,
    )


def _category_from_event_type(
    normalized_event_type: str,
    *,
    exact_map: dict[str, str] | None,
) -> str:
    if exact_map is not None and normalized_event_type in exact_map:
        return exact_map[normalized_event_type]

    if normalized_event_type.startswith("spawn.") or normalized_event_type.startswith(
        "meridian.spawn."
    ):
        return "sub-run"
    if any(token in normalized_event_type for token in ("error", "fail", "warning", "warn")):
        return "error"
    if any(token in normalized_event_type for token in ("tool", "function_call", "call_tool")):
        return "tool-use"
    if any(token in normalized_event_type for token in ("think", "reasoning", "reason")):
        return "thinking"
    if any(token in normalized_event_type for token in ("assistant", "message", "response")):
        return "assistant"
    if any(
        token in normalized_event_type
        for token in (
            "start",
            "started",
            "finish",
            "finished",
            "complete",
            "completed",
            "done",
            "result",
        )
    ):
        return "lifecycle"
    return "progress"


def _read_json_artifact(
    artifacts: ArtifactStore, spawn_id: SpawnId, filename: str
) -> dict[str, object] | None:
    artifact_key = ArtifactKey(f"{spawn_id}/{filename}")
    if not artifacts.exists(artifact_key):
        return None
    raw = artifacts.get(artifact_key)
    try:
        payload_obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload_obj, dict):
        return cast("dict[str, object]", payload_obj)
    return None


def unwrap_event_payload(line: dict[str, object]) -> dict[str, object]:
    """Extract the effective payload from a harness JSONL artifact line.

    Handles both envelope format (streaming drain) and raw format (legacy).
    """
    if "event_type" in line and "payload" in line:
        payload = line["payload"]
        if isinstance(payload, dict):
            return cast("dict[str, object]", payload)
    return line


class _UsageCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None


TOKEN_KEY_PAIRS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "output_tokens"),
    ("input", "output"),
    ("prompt_tokens", "completion_tokens"),
    ("prompt_token_count", "completion_token_count"),
    ("inputTokenCount", "outputTokenCount"),
)
COST_KEYS: tuple[str, ...] = (
    "total_cost_usd",
    "cost_usd",
    "cost",
    "total_cost",
    "totalCostUsd",
)


def _coerce_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def coerce_optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("$"):
            stripped = stripped[1:]
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def iter_nested_dicts(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        payload = cast("dict[str, object]", value)
        found.append(payload)
        for nested in payload.values():
            found.extend(iter_nested_dicts(nested))
    elif isinstance(value, list):
        for item in cast("list[object]", value):
            found.extend(iter_nested_dicts(item))
    return found


def _extract_cost(payload: dict[str, object]) -> float | None:
    for key in COST_KEYS:
        value = coerce_optional_float(payload.get(key))
        if value is not None:
            return value
    return None


def _candidate_from_payload(payload: dict[str, object]) -> _UsageCandidate:
    for input_key, output_key in TOKEN_KEY_PAIRS:
        if input_key not in payload and output_key not in payload:
            continue
        input_tokens = _coerce_optional_int(payload.get(input_key))
        output_tokens = _coerce_optional_int(payload.get(output_key))
        cost = _extract_cost(payload)
        return _UsageCandidate(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_cost_usd=cost,
        )

    return _UsageCandidate(total_cost_usd=_extract_cost(payload))


def _candidate_token_score(candidate: _UsageCandidate) -> int:
    score = 0
    if candidate.input_tokens is not None:
        score += 1
    if candidate.output_tokens is not None:
        score += 1
    return score


def _iter_json_lines_artifact(
    artifacts: ArtifactStore, spawn_id: SpawnId, filename: str
) -> list[dict[str, object]]:
    artifact_key = ArtifactKey(f"{spawn_id}/{filename}")
    if not artifacts.exists(artifact_key):
        if filename == OUTPUT_FILENAME:
            artifact_key = ArtifactKey(f"{spawn_id}/{HISTORY_FILENAME}")
            if not artifacts.exists(artifact_key):
                return []
        else:
            return []

    raw = artifacts.get(artifact_key)
    decoded = raw.decode("utf-8", errors="ignore")
    payloads: list[dict[str, object]] = []
    for line in decoded.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload_obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload_obj, dict):
            line_payload = cast("dict[str, object]", payload_obj)
            payload = unwrap_event_payload(line_payload)
            if "event_type" in line_payload and "event_type" not in payload:
                payload = dict(payload)
                payload["event_type"] = line_payload["event_type"]
            payloads.append(payload)
    return payloads


def _extract_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        parts = [_extract_text(item) for item in cast("list[object]", value)]
        return "\n".join(part for part in parts if part).strip()

    if isinstance(value, dict):
        payload = cast("dict[str, object]", value)
        parts: list[str] = []
        for key in ("text", "message", "output", "content", "result"):
            if key in payload:
                text = _extract_text(payload[key])
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()

    return ""


def extract_codex_report(artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
    last_message: str | None = None
    for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
        event_type = (
            str(payload.get("event_type", payload.get("event", payload.get("type", ""))))
            .strip()
            .lower()
            .replace("/", ".")
        )
        if event_type != "item.completed":
            continue

        item = payload.get("item")
        if not isinstance(item, dict):
            continue

        item_payload = cast("dict[str, object]", item)
        item_type = str(item_payload.get("type", "")).strip().lower().replace("_", "")
        if item_type != "agentmessage":
            continue

        text = _extract_text(item_payload.get("text"))
        if text:
            last_message = text
    return last_message


def _extract_claude_assistant_content(payload: dict[str, object]) -> str:
    content = _extract_text(payload.get("content"))
    if content:
        return content

    message = payload.get("message")
    if isinstance(message, dict):
        return _extract_text(cast("dict[str, object]", message).get("content"))
    return ""


def extract_claude_report(artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
    result_text: str | None = None
    assistant_text: str | None = None

    for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
        event_type = str(payload.get("type", payload.get("event", ""))).strip().lower()
        if event_type == "result":
            candidate = _extract_text(payload.get("result"))
            if candidate:
                result_text = candidate
            continue

        if event_type == "assistant":
            candidate = _extract_claude_assistant_content(payload)
            if candidate:
                assistant_text = candidate

    return result_text or assistant_text


_OPENCODE_SESSION_ID_JSON_KEYS = ("session_id", "sessionId", "sessionID", "id")


def _opencode_event_type(payload: dict[str, object]) -> str:
    return (
        str(payload.get("event_type", payload.get("event", payload.get("type", ""))))
        .strip()
        .lower()
    )


def _opencode_message_payload(payload: dict[str, object]) -> dict[str, object]:
    nested = payload.get("payload")
    if isinstance(nested, dict):
        return cast("dict[str, object]", nested)
    return payload


def _extract_opencode_report_from_stream(payloads: list[dict[str, object]]) -> str | None:
    assistant_message_ids: set[str] = set()
    part_text_by_message: dict[str, list[str]] = {}
    last_assistant_message_id: str | None = None
    last_embedded_message: str | None = None

    for payload in payloads:
        event_type = _opencode_event_type(payload)
        message_payload = _opencode_message_payload(payload)
        inner_type = _opencode_event_type(message_payload)
        effective_type = event_type or inner_type

        if effective_type == "message.updated":
            properties_obj = message_payload.get("properties")
            if not isinstance(properties_obj, dict):
                continue
            properties = cast("dict[str, object]", properties_obj)

            info_obj = properties.get("info")
            if not isinstance(info_obj, dict):
                continue
            info = cast("dict[str, object]", info_obj)

            role = str(info.get("role", "")).strip().lower()
            message_id = str(info.get("id", "")).strip()
            if role == "assistant" and message_id:
                assistant_message_ids.add(message_id)
                last_assistant_message_id = message_id

            if role != "assistant":
                continue

            parts_obj = info.get("parts")
            if not isinstance(parts_obj, list):
                continue
            parts = cast("list[object]", parts_obj)

            text_chunks: list[str] = []
            for part_obj in parts:
                if not isinstance(part_obj, dict):
                    continue
                part = cast("dict[str, object]", part_obj)
                if str(part.get("type", "")).strip().lower() != "text":
                    continue
                text_chunks.append(str(part.get("text", "")))
            message = "".join(chunk for chunk in text_chunks if chunk).strip()
            if message:
                last_embedded_message = message
            continue

        if effective_type != "message.part.updated":
            continue

        properties_obj = message_payload.get("properties")
        if not isinstance(properties_obj, dict):
            continue
        properties = cast("dict[str, object]", properties_obj)

        part_obj = properties.get("part")
        if not isinstance(part_obj, dict):
            continue
        part = cast("dict[str, object]", part_obj)
        if str(part.get("type", "")).strip().lower() != "text":
            continue

        message_id = str(part.get("messageID", part.get("message_id", ""))).strip()
        if not message_id or message_id not in assistant_message_ids:
            continue

        text = str(part.get("text", "")).strip()
        if not text:
            continue
        part_text_by_message.setdefault(message_id, []).append(text)
        last_assistant_message_id = message_id

    if last_assistant_message_id:
        chunks = part_text_by_message.get(last_assistant_message_id)
        if chunks:
            joined = "".join(chunks).strip()
            if joined:
                return joined

    return last_embedded_message


def extract_opencode_report(artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
    payloads = _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
    report = _extract_opencode_report_from_stream(payloads)
    if report:
        return report

    session_id = extract_session_id_from_artifacts_with_patterns(
        artifacts,
        spawn_id,
        json_keys=_OPENCODE_SESSION_ID_JSON_KEYS,
    )
    if not session_id:
        return None

    from meridian.lib.harness.opencode_storage import resolve_opencode_session_file
    from meridian.lib.harness.opencode_transcript import (
        extract_last_assistant_report_from_session_path,
    )

    session_path = resolve_opencode_session_file(session_id=session_id)
    if session_path is None:
        return None
    return extract_last_assistant_report_from_session_path(session_path)


def extract_usage_from_artifacts(artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
    candidates: list[_UsageCandidate] = []

    for filename in ("tokens.json", "usage.json"):
        payload = _read_json_artifact(artifacts, spawn_id, filename)
        if payload is None:
            continue
        for nested in iter_nested_dicts(payload):
            candidates.append(_candidate_from_payload(nested))

    for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
        for nested in iter_nested_dicts(payload):
            candidates.append(_candidate_from_payload(nested))

    if not candidates:
        return TokenUsage()

    best_tokens = max(candidates, key=_candidate_token_score)
    best_cost = next(
        (
            candidate.total_cost_usd
            for candidate in candidates
            if candidate.total_cost_usd is not None
        ),
        None,
    )

    if _candidate_token_score(best_tokens) == 0 and best_cost is None:
        return TokenUsage()

    return TokenUsage(
        input_tokens=best_tokens.input_tokens,
        output_tokens=best_tokens.output_tokens,
        total_cost_usd=best_cost,
    )


def extract_session_id_from_artifacts_with_patterns(
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    *,
    json_keys: tuple[str, ...] = ("session_id", "sessionId"),
    text_patterns: tuple[re.Pattern[str], ...] = (),
) -> str | None:
    key = ArtifactKey(f"{spawn_id}/session_id.txt")
    if artifacts.exists(key):
        raw = artifacts.get(key)
        session_id = raw.decode("utf-8", errors="ignore").strip()
        if session_id:
            return session_id

    history_key = ArtifactKey(f"{spawn_id}/{HISTORY_FILENAME}")
    output_key = ArtifactKey(f"{spawn_id}/{OUTPUT_FILENAME}")
    if artifacts.exists(history_key):
        artifact_name = HISTORY_FILENAME
        raw_output = artifacts.get(history_key).decode("utf-8", errors="ignore")
    elif artifacts.exists(output_key):
        artifact_name = OUTPUT_FILENAME
        raw_output = artifacts.get(output_key).decode("utf-8", errors="ignore")
    else:
        return None

    for payload in _iter_json_lines_artifact(artifacts, spawn_id, artifact_name):
        for nested in iter_nested_dicts(payload):
            for key_name in json_keys:
                value = nested.get(key_name)
                if not isinstance(value, str):
                    continue
                session_id = value.strip()
                if session_id:
                    return session_id

    if not text_patterns:
        return None

    for line in raw_output.splitlines():
        for pattern in text_patterns:
            match = pattern.search(line)
            if match is None:
                continue
            session_id = match.group(1).strip()
            if session_id:
                return session_id
    return None
