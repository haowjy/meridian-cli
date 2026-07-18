"""Shared Pi lifecycle event parsing helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent

PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION: Final[int] = 1
PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES: Final[tuple[str, ...]] = (
    "meridian.quiescence.",
)
PI_LIFECYCLE_EVENT_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {"meridian.quiescence.ready"}
)
PI_PHASE_EVENT_TYPE: Final[str] = "meridian.pi.lifecycle.phase"
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


def build_pi_phase_event(
    spawn_id: SpawnId,
    connection: HarnessConnection[Any],
    phase: str,
    **data: object,
) -> RawHarnessEvent:
    """Build one Meridian-authored Pi lifecycle phase event."""

    payload: dict[str, object] = {
        "type": PI_PHASE_EVENT_TYPE,
        "phase": phase,
        "spawn_id": str(spawn_id),
        "schema_version": PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION,
    }
    payload.update((key, value) for key, value in data.items() if value is not None)
    return RawHarnessEvent(
        event_type=PI_PHASE_EVENT_TYPE,
        harness_id=connection.harness_id.value,
        payload=payload,
        raw_text=None,
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


def pi_lifecycle_event_label_candidates(event: RawHarnessEvent) -> tuple[str, ...]:
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


__all__ = [
    "PI_CANONICAL_LIFECYCLE_TYPE_PREFIXES",
    "PI_LIFECYCLE_EVENT_ALLOWLIST",
    "PI_PHASE_EVENT_TYPE",
    "PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION",
    "build_pi_phase_event",
    "has_unsupported_pi_lifecycle_schema_version",
    "normalize_pi_lifecycle_label",
    "pi_lifecycle_event_label_candidates",
    "redact_pi_command_for_history",
    "unsupported_pi_schema_version_error",
]
