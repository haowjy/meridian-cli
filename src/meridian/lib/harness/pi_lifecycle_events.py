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
PI_LIFECYCLE_EVENT_ALLOWLIST: Final[frozenset[str]] = frozenset(
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
PI_CANONICAL_SUBSPAWN_START_EVENTS: Final[frozenset[str]] = frozenset(
    {"meridian.subspawn.start"}
)
PI_LEGACY_SUBSPAWN_START_EVENTS: Final[frozenset[str]] = frozenset(
    {"meridian_subspawn_start"}
)
PI_SUBSPAWN_START_EVENTS: Final[frozenset[str]] = frozenset(
    PI_CANONICAL_SUBSPAWN_START_EVENTS | PI_LEGACY_SUBSPAWN_START_EVENTS
)
PI_CANONICAL_SUBSPAWN_END_EVENTS: Final[frozenset[str]] = frozenset(
    {"meridian.subspawn.end"}
)
PI_LEGACY_SUBSPAWN_END_EVENTS: Final[frozenset[str]] = frozenset(
    {"meridian_subspawn_end"}
)
PI_SUBSPAWN_END_EVENTS: Final[frozenset[str]] = frozenset(
    PI_CANONICAL_SUBSPAWN_END_EVENTS | PI_LEGACY_SUBSPAWN_END_EVENTS
)
PI_CANONICAL_SUBSPAWN_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "meridian.subspawn.start",
        "meridian.subspawn.end",
    }
)
PI_NOTIFICATION_QUEUED_EVENTS: Final[frozenset[str]] = frozenset(
    {"meridian.notification.queued", "meridian_notification_queued"}
)
PI_NOTIFICATION_DELIVERED_EVENTS: Final[frozenset[str]] = frozenset(
    {"meridian.notification.delivered", "meridian_notification_delivered"}
)
PI_NOTIFICATION_COMPLETED_EVENTS: Final[frozenset[str]] = frozenset(
    {"meridian.notification.completed", "meridian_notification_completed"}
)
PI_NOTIFICATION_FAILED_EVENTS: Final[frozenset[str]] = frozenset(
    {"meridian.notification.failed", "meridian_notification_failed"}
)
PI_CANONICAL_NOTIFICATION_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "meridian.notification.queued",
        "meridian.notification.delivered",
        "meridian.notification.completed",
        "meridian.notification.failed",
    }
)
PI_CANONICAL_DEDUP_LIFECYCLE_EVENTS: Final[frozenset[str]] = frozenset(
    PI_LIFECYCLE_EVENT_ALLOWLIST
)
PI_PHASE_EVENT_TYPE: Final[str] = "meridian.pi.lifecycle.phase"
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


def normalize_pi_lifecycle_label(raw: str) -> str:
    return raw.strip().lower().replace("-", "_").replace("/", ".")


def pi_lifecycle_event_label_candidates(event: HarnessEvent) -> tuple[str, ...]:
    candidates: list[str] = []
    event_type = normalize_pi_lifecycle_label(event.event_type)
    if event_type:
        candidates.append(event_type)
    payload_type = event.payload.get("type")
    if isinstance(payload_type, str):
        payload_label = normalize_pi_lifecycle_label(payload_type)
        if payload_label and payload_label not in candidates:
            candidates.append(payload_label)
    return tuple(candidates)


def is_legacy_pi_notification_label(labels: set[str]) -> bool:
    if labels & PI_CANONICAL_NOTIFICATION_EVENTS:
        return False
    return any(
        label.startswith("meridian_notification_")
        and label not in PI_CANONICAL_NOTIFICATION_EVENTS
        for label in labels
    )


def pi_subspawn_id(payload: dict[str, object]) -> str | None:
    for key in ("subspawn_id", "spawn_id", "child_spawn_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def pi_notification_id(payload: dict[str, object]) -> str | None:
    for key in ("notification_id", "correlation_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def pi_wait_policy_is_tracked(payload: dict[str, object]) -> bool:
    raw_policy = payload.get("wait_policy")
    if not isinstance(raw_policy, str):
        return True
    return raw_policy.strip().lower() != "detached"


def pi_correlation_id(payload: dict[str, object]) -> str | None:
    value = payload.get("correlation_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def pi_subspawn_pid(payload: dict[str, object]) -> int | None:
    for key in ("pid", "child_pid", "process_id"):
        value = _coerce_int(payload.get(key))
        if value is not None:
            return value
    return None


def pi_subspawn_pgid(payload: dict[str, object]) -> int | None:
    for key in ("pgid", "process_group_id"):
        value = _coerce_int(payload.get(key))
        if value is not None:
            return value
    return None


def pi_notification_failure_error(payload: dict[str, object]) -> str:
    reason = payload.get("reason")
    error = payload.get("error")
    reason_text = reason.strip() if isinstance(reason, str) else ""
    error_text = error.strip() if isinstance(error, str) else ""
    if reason_text and error_text:
        return f"pi_notification_failed:{reason_text}:{error_text}"
    if reason_text:
        return f"pi_notification_failed:{reason_text}"
    if error_text:
        return f"pi_notification_failed:{error_text}"
    return "pi_notification_failed"


def unsupported_pi_schema_version_error(
    labels: set[str],
    payload: dict[str, object],
) -> str | None:
    canonical_lifecycle_labels = {
        label
        for label in labels
        if label.startswith(PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES)
    }
    if not canonical_lifecycle_labels:
        return None

    raw_schema_version = payload.get("schema_version")
    if raw_schema_version is None:
        return None
    schema_version = _coerce_int(raw_schema_version)
    if schema_version is None:
        return "pi_lifecycle_tracking_invalidated:unsupported_schema_version:unknown"
    if schema_version != PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION:
        return (
            "pi_lifecycle_tracking_invalidated:unsupported_schema_version:"
            f"{schema_version}"
        )
    return None


def canonical_pi_lifecycle_label(
    labels: set[str],
    canonical_labels: frozenset[str],
) -> str:
    matched = sorted(label for label in labels if label in canonical_labels)
    if matched:
        return matched[0]
    return "unknown"


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


def _validate_lifecycle_event_payload(
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

    if event_type in PI_CANONICAL_SUBSPAWN_EVENTS:
        subspawn_id = payload.get("subspawn_id")
        if not isinstance(subspawn_id, str) or not subspawn_id.strip():
            return "missing_subspawn_id"

    if event_type in PI_CANONICAL_NOTIFICATION_EVENTS:
        notification_id = payload.get("notification_id")
        if not isinstance(notification_id, str) or not notification_id.strip():
            return "missing_notification_id"

    return None


def parse_pi_lifecycle_event_line(
    line: str,
    *,
    expected_parent_spawn_id: SpawnId | str,
    harness_id: str,
) -> HarnessEvent | None:
    """Parse one lifecycle side-channel line into a lifecycle or parse-error event."""

    payload_text = line.strip()
    if not payload_text:
        return None
    try:
        payload_obj = json.loads(payload_text)
    except json.JSONDecodeError:
        return _lifecycle_parse_error_event(
            reason="malformed_json",
            error="malformed_json",
            raw_line=payload_text,
            harness_id=harness_id,
        )
    if not isinstance(payload_obj, dict):
        return _lifecycle_parse_error_event(
            reason="non_object",
            error="non_object",
            raw_line=payload_text,
            harness_id=harness_id,
        )

    payload = cast("dict[str, object]", payload_obj)
    event_type = payload.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        return _lifecycle_parse_error_event(
            reason="missing_type",
            error="missing_type",
            raw_line=payload_text,
            harness_id=harness_id,
        )

    normalized_type = event_type.strip()
    if normalized_type not in PI_LIFECYCLE_EVENT_ALLOWLIST:
        return _lifecycle_parse_error_event(
            reason="unsupported_type",
            error="unsupported_type",
            raw_type=normalized_type,
            raw_line=payload_text,
            harness_id=harness_id,
        )

    invalid_reason = _validate_lifecycle_event_payload(
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
    "PI_CANONICAL_DEDUP_LIFECYCLE_EVENTS",
    "PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES",
    "PI_CANONICAL_NOTIFICATION_EVENTS",
    "PI_CANONICAL_SUBSPAWN_END_EVENTS",
    "PI_CANONICAL_SUBSPAWN_EVENTS",
    "PI_CANONICAL_SUBSPAWN_START_EVENTS",
    "PI_LEGACY_SUBSPAWN_END_EVENTS",
    "PI_LEGACY_SUBSPAWN_START_EVENTS",
    "PI_LIFECYCLE_EVENT_ALLOWLIST",
    "PI_NOTIFICATION_COMPLETED_EVENTS",
    "PI_NOTIFICATION_DELIVERED_EVENTS",
    "PI_NOTIFICATION_FAILED_EVENTS",
    "PI_NOTIFICATION_QUEUED_EVENTS",
    "PI_PHASE_EVENT_TYPE",
    "PI_SUBSPAWN_END_EVENTS",
    "PI_SUBSPAWN_START_EVENTS",
    "PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION",
    "canonical_pi_lifecycle_label",
    "has_unsupported_pi_lifecycle_schema_version",
    "is_legacy_pi_notification_label",
    "normalize_pi_lifecycle_label",
    "parse_pi_lifecycle_event_line",
    "pi_correlation_id",
    "pi_lifecycle_event_label_candidates",
    "pi_notification_failure_error",
    "pi_notification_id",
    "pi_subspawn_id",
    "pi_subspawn_pgid",
    "pi_subspawn_pid",
    "pi_wait_policy_is_tracked",
    "redact_pi_command_for_history",
    "unsupported_pi_schema_version_error",
]
