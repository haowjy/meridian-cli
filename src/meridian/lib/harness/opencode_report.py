"""OpenCode-owned session id and report extraction."""

from __future__ import annotations

from typing import cast

from meridian.lib.harness.common import (
    extract_text,
)


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


def _opencode_message_payload(payload: dict[str, object]) -> dict[str, object]:
    nested = payload.get("payload")
    if isinstance(nested, dict):
        return cast("dict[str, object]", nested)
    return payload
