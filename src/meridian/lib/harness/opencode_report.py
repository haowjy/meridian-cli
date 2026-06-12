"""OpenCode-owned session id and report extraction."""

from __future__ import annotations

from typing import cast

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.harness.common import (
    extract_session_id_from_artifacts_with_patterns,
    extract_text,
    iter_json_lines_artifact,
    read_session_id_artifact,
)
from meridian.lib.launch.constants import OUTPUT_FILENAME

_OPENCODE_SESSION_ID_JSON_KEYS = ("session_id", "sessionId", "sessionID", "id")


def extract_opencode_session_id(payload: dict[str, object]) -> str | None:
    """Read an OpenCode session id from an event payload."""

    normalized_payload = _opencode_message_payload(payload)
    for candidate in _opencode_session_id_candidates(normalized_payload):
        session_id = extract_text(candidate)
        if session_id:
            return session_id
    return None


def _opencode_session_id_candidates(payload: dict[str, object]) -> list[object]:
    candidates: list[object] = []
    for key in ("sessionID", "sessionId", "session_id"):
        if key in payload:
            candidates.append(payload[key])

    properties_obj = payload.get("properties")
    if isinstance(properties_obj, dict):
        properties = cast("dict[str, object]", properties_obj)
        for key in ("sessionID", "sessionId", "session_id"):
            if key in properties:
                candidates.append(properties[key])
        for nested_key in ("info", "part", "message"):
            nested_obj = properties.get(nested_key)
            if isinstance(nested_obj, dict):
                nested = cast("dict[str, object]", nested_obj)
                for key in ("sessionID", "sessionId", "session_id"):
                    if key in nested:
                        candidates.append(nested[key])

    for nested_key in ("info", "part", "message"):
        nested_obj = payload.get(nested_key)
        if isinstance(nested_obj, dict):
            nested = cast("dict[str, object]", nested_obj)
            for key in ("sessionID", "sessionId", "session_id"):
                if key in nested:
                    candidates.append(nested[key])

    session_obj = payload.get("session")
    if isinstance(session_obj, dict):
        session = cast("dict[str, object]", session_obj)
        for key in ("id", "sessionID", "sessionId", "session_id"):
            if key in session:
                candidates.append(session[key])

    return candidates


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


def _opencode_message_role(payload: dict[str, object]) -> str:
    message_payload = _opencode_message_payload(payload)
    properties_obj = message_payload.get("properties")
    if not isinstance(properties_obj, dict):
        return ""
    properties = cast("dict[str, object]", properties_obj)
    info_obj = properties.get("info")
    if not isinstance(info_obj, dict):
        return ""
    info = cast("dict[str, object]", info_obj)
    return str(info.get("role", "")).strip().lower()


def _resolve_opencode_primary_session_id(payloads: list[dict[str, object]]) -> str | None:
    for payload in payloads:
        event_type = _opencode_event_type(payload)
        message_payload = _opencode_message_payload(payload)
        inner_type = _opencode_event_type(message_payload)
        effective_type = event_type or inner_type
        if effective_type != "message.updated":
            continue
        if _opencode_message_role(payload) != "user":
            continue
        session_id = extract_opencode_session_id(payload)
        if session_id:
            return session_id
    return None


def _opencode_event_matches_session(
    payload: dict[str, object],
    *,
    primary_session_id: str | None,
) -> bool:
    if primary_session_id is None:
        return True
    session_id = extract_opencode_session_id(payload)
    return session_id == primary_session_id


def _extract_opencode_report_from_stream(
    payloads: list[dict[str, object]],
    *,
    primary_session_id: str | None = None,
) -> str | None:
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
            if not _opencode_event_matches_session(
                payload,
                primary_session_id=primary_session_id,
            ):
                continue
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

        if not _opencode_event_matches_session(payload, primary_session_id=primary_session_id):
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
    payloads = iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
    primary_session_id = read_session_id_artifact(
        artifacts,
        spawn_id,
    ) or _resolve_opencode_primary_session_id(payloads)
    report = _extract_opencode_report_from_stream(
        payloads,
        primary_session_id=primary_session_id,
    )
    if report:
        return report

    session_id = primary_session_id or extract_session_id_from_artifacts_with_patterns(
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


__all__ = ["extract_opencode_report", "extract_opencode_session_id"]
