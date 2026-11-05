"""Shared helpers for harness adapters."""

import json
import math
from typing import cast

from meridian.lib.harness.adapter import StreamEvent

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


def coerce_optional_int(value: object) -> int | None:
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
        return None
    if isinstance(value, int | float):
        candidate = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("$"):
            stripped = stripped[1:]
        candidate = stripped
    else:
        return None
    try:
        parsed = float(candidate)
    except (ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


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


def extract_text(value: object) -> str:
    return _extract_text(value)


def extract_codex_thread_id(payload: dict[str, object]) -> str | None:
    """Read a Codex thread id from a turn/item notification payload."""

    thread_obj = payload.get("thread")
    if isinstance(thread_obj, dict):
        thread_id = _extract_text(cast("dict[str, object]", thread_obj).get("id"))
        if thread_id:
            return thread_id

    for key in ("threadId", "thread_id"):
        thread_id = _extract_text(payload.get(key))
        if thread_id:
            return thread_id
    return None
