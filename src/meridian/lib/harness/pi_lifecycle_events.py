"""Shared Pi lifecycle event parsing helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final, cast

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessEvent

PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION: Final[int] = 1
PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES: Final[tuple[str, ...]] = (
    "meridian.subspawn.",
    "meridian.notification.",
    "meridian.quiescence.",
)
PI_STDERR_LIFECYCLE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "meridian.subspawn.start",
        "meridian.subspawn.end",
        "meridian.notification.queued",
        "meridian.notification.delivered",
        "meridian.notification.completed",
        "meridian.notification.failed",
        "meridian.quiescence.ready",
    }
)
_PI_STDERR_SUBSPAWN_TYPES: Final[frozenset[str]] = frozenset(
    {
        "meridian.subspawn.start",
        "meridian.subspawn.end",
    }
)
_PI_STDERR_NOTIFICATION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "meridian.notification.queued",
        "meridian.notification.delivered",
        "meridian.notification.completed",
        "meridian.notification.failed",
    }
)
_PARSE_ERROR_RAW_LINE_LIMIT: Final[int] = 2048
_REDACTED_ARG_VALUE: Final[str] = "<redacted>"
_SECRET_FLAG_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "authorization",
        "apikey",
        "key",
        "auth",
    }
)


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.startswith(("+", "-")):
            sign = raw[0]
            digits = raw[1:]
            if digits.isdigit():
                return int(f"{sign}{digits}")
            return None
        if raw.isdigit():
            return int(raw)
    return None


def _truncate_parse_error_raw_line(raw_line: str) -> str:
    if len(raw_line) <= _PARSE_ERROR_RAW_LINE_LIMIT:
        return raw_line
    truncated = raw_line[:_PARSE_ERROR_RAW_LINE_LIMIT]
    return f"{truncated}…<truncated>"


def _secret_flag_token(token: str) -> str | None:
    if not token.startswith("-"):
        return None
    flag = token.split("=", 1)[0]
    normalized = flag.lstrip("-").strip().lower().replace("_", "-")
    if not normalized:
        return None
    segments = [segment for segment in normalized.split("-") if segment]
    if not segments:
        return None
    has_secret_segment = any(segment in _SECRET_FLAG_SEGMENTS for segment in segments)
    if not has_secret_segment:
        return None
    return flag


def redact_pi_command_for_history(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for token in command:
        if redact_next:
            redacted.append(_REDACTED_ARG_VALUE)
            redact_next = False
            continue
        flag_token = _secret_flag_token(token)
        if flag_token is None:
            redacted.append(token)
            continue
        if "=" in token:
            redacted.append(f"{flag_token}={_REDACTED_ARG_VALUE}")
            continue
        redacted.append(token)
        redact_next = True
    return redacted


def _lifecycle_parse_error_event(
    *,
    reason: str,
    raw_line: str,
    harness_id: str,
    error: str | None = None,
    raw_type: str | None = None,
) -> HarnessEvent:
    payload: dict[str, object] = {
        "type": "meridian.lifecycle.parse_error",
        "schema_version": PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION,
        "reason": reason,
        "raw_line": _truncate_parse_error_raw_line(raw_line),
    }
    if error is not None:
        payload["error"] = error
    if raw_type is not None:
        payload["raw_type"] = raw_type
    return HarnessEvent(
        event_type="meridian.lifecycle.parse_error",
        payload=payload,
        harness_id=harness_id,
        raw_text=raw_line,
    )


def has_unsupported_pi_lifecycle_schema_version(
    *,
    event_type: str,
    payload: dict[str, object],
) -> bool:
    if not event_type.startswith(PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES):
        return False
    raw_schema_version = payload.get("schema_version")
    if raw_schema_version is None:
        return False
    schema_version = _coerce_int(raw_schema_version)
    if schema_version is None:
        return True
    return schema_version != PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION


def _validate_stderr_lifecycle_payload(
    *,
    event_type: str,
    payload: dict[str, object],
    expected_parent_spawn_id: str,
) -> str | None:
    schema_version = _coerce_int(payload.get("schema_version"))
    if schema_version != PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION:
        return "unsupported_schema_version"

    parent_spawn_id = payload.get("parent_spawn_id")
    if not isinstance(parent_spawn_id, str) or not parent_spawn_id.strip():
        return "missing_parent_spawn_id"
    if parent_spawn_id.strip() != expected_parent_spawn_id:
        return "parent_spawn_id_mismatch"

    correlation_id = payload.get("correlation_id")
    if not isinstance(correlation_id, str) or not correlation_id.strip():
        return "missing_correlation_id"

    emitted_at_ms = _coerce_int(payload.get("emitted_at_ms"))
    if emitted_at_ms is None:
        return "invalid_emitted_at_ms"

    if event_type in _PI_STDERR_SUBSPAWN_TYPES:
        subspawn_id = payload.get("subspawn_id")
        if not isinstance(subspawn_id, str) or not subspawn_id.strip():
            return "missing_subspawn_id"

    if event_type in _PI_STDERR_NOTIFICATION_TYPES:
        notification_id = payload.get("notification_id")
        if not isinstance(notification_id, str) or not notification_id.strip():
            return "missing_notification_id"

    return None


def parse_pi_stderr_lifecycle_line(
    line: str,
    *,
    expected_parent_spawn_id: SpawnId | str,
    harness_id: str,
    enabled: bool,
) -> HarnessEvent | None:
    """Parse one stderr line into a lifecycle event or diagnostic parse-error event."""

    if not enabled:
        return None
    payload_text = line.strip()
    if not payload_text:
        return None
    try:
        payload_obj = json.loads(payload_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload_obj, dict):
        return None

    payload = cast("dict[str, object]", payload_obj)
    event_type = payload.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        return None

    normalized_type = event_type.strip()
    if normalized_type not in PI_STDERR_LIFECYCLE_ALLOWLIST:
        return None

    invalid_reason = _validate_stderr_lifecycle_payload(
        event_type=normalized_type,
        payload=payload,
        expected_parent_spawn_id=str(expected_parent_spawn_id),
    )
    if invalid_reason is not None:
        return _lifecycle_parse_error_event(
            reason=invalid_reason,
            error=invalid_reason,
            raw_type=normalized_type,
            raw_line=payload_text,
            harness_id=harness_id,
        )

    return HarnessEvent(
        event_type=normalized_type,
        payload=payload,
        harness_id=harness_id,
        raw_text=line,
    )


__all__ = [
    "PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES",
    "PI_STDERR_LIFECYCLE_ALLOWLIST",
    "PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION",
    "has_unsupported_pi_lifecycle_schema_version",
    "parse_pi_stderr_lifecycle_line",
    "redact_pi_command_for_history",
]
