"""Telemetry v1 envelope and event registry."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from meridian.lib.core.spawn_lifecycle import FAILURE_SPAWN_STATUSES

VALID_DOMAINS = frozenset({"spawn", "chat", "server", "work", "runtime", "usage"})
VALID_SEVERITIES = frozenset({"debug", "info", "warning", "error"})
VALID_CONCERNS = frozenset({"operational", "error", "usage"})

Domain = Literal["spawn", "chat", "server", "work", "runtime", "usage"]
Severity = Literal["debug", "info", "warning", "error"]
Concern = Literal["operational", "error", "usage"]


@dataclass(frozen=True)
class TelemetryEnvelope:
    """8-field v1 telemetry envelope."""

    v: int
    ts: str
    domain: str
    event: str
    scope: str
    severity: str | None = None
    ids: dict[str, str] | None = None
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-ready dict, omitting absent optional fields."""
        payload: dict[str, Any] = {
            "v": self.v,
            "ts": self.ts,
            "domain": self.domain,
            "event": self.event,
            "scope": self.scope,
        }
        if self.severity is not None:
            payload["severity"] = self.severity
        if self.ids is not None:
            payload["ids"] = self.ids
        if self.data is not None:
            payload["data"] = self.data
        return payload


class EventDefinition(TypedDict):
    """Normative metadata for a telemetry event."""

    domain: Domain
    concerns: tuple[Concern, ...]
    ids: tuple[str, ...]


def _spawn_event_definition(status: str) -> EventDefinition:
    concerns: tuple[Concern, ...] = (
        ("operational", "error") if status in FAILURE_SPAWN_STATUSES else ("operational",)
    )
    return {"domain": "spawn", "concerns": concerns, "ids": ("spawn_id",)}


EVENT_REGISTRY: dict[str, EventDefinition] = {
    # Spawn domain: sparse correlation markers.
    "spawn.process_exited": {"domain": "spawn", "concerns": ("operational",), "ids": ("spawn_id",)},
    "spawn.succeeded": _spawn_event_definition("succeeded"),
    "spawn.failed": _spawn_event_definition("failed"),
    "spawn.cancelled": _spawn_event_definition("cancelled"),
    "spawn.timed_out": _spawn_event_definition("timed_out"),
    # Chat domain: dead-zone events.
    "chat.http.request_completed": {
        "domain": "chat",
        "concerns": ("operational",),
        "ids": ("chat_id", "command_id"),
    },
    "chat.ws.connected": {
        "domain": "chat",
        "concerns": ("operational", "usage"),
        "ids": ("chat_id",),
    },
    "chat.ws.disconnected": {"domain": "chat", "concerns": ("operational",), "ids": ("chat_id",)},
    "chat.ws.rejected": {
        "domain": "chat",
        "concerns": ("operational", "error"),
        "ids": ("chat_id",),
    },
    "chat.command.dispatched": {
        "domain": "chat",
        "concerns": ("operational", "usage"),
        "ids": ("chat_id", "command_id"),
    },
    "chat.runtime.stopping": {"domain": "chat", "concerns": ("operational",), "ids": ("chat_id",)},
    "chat.runtime.stopped": {"domain": "chat", "concerns": ("operational",), "ids": ("chat_id",)},
    # Server domain.
    "dev_frontend.launched": {"domain": "server", "concerns": ("operational", "usage"), "ids": ()},
    "dev_frontend.ready": {"domain": "server", "concerns": ("operational", "usage"), "ids": ()},
    "dev_frontend.readiness_timeout": {
        "domain": "server",
        "concerns": ("operational", "error"),
        "ids": (),
    },
    "dev_frontend.exited": {"domain": "server", "concerns": ("operational",), "ids": ()},
    "mcp.command.invoked": {
        "domain": "server",
        "concerns": ("operational", "usage"),
        "ids": ("request_id", "work_id", "spawn_id"),
    },
    # Work domain.
    "work.started": {"domain": "work", "concerns": ("operational", "usage"), "ids": ("work_id",)},
    "work.updated": {"domain": "work", "concerns": ("operational",), "ids": ("work_id",)},
    "work.done": {"domain": "work", "concerns": ("operational", "usage"), "ids": ("work_id",)},
    "work.deleted": {"domain": "work", "concerns": ("operational",), "ids": ("work_id",)},
    "work.reopened": {"domain": "work", "concerns": ("operational", "usage"), "ids": ("work_id",)},
    "work.renamed": {"domain": "work", "concerns": ("operational",), "ids": ("work_id",)},
    # Runtime domain: self-observability.
    "runtime.telemetry.dropped": {
        "domain": "runtime",
        "concerns": ("operational", "error"),
        "ids": (),
    },
    "runtime.telemetry.sink_failed": {
        "domain": "runtime",
        "concerns": ("operational", "error"),
        "ids": (),
    },
    "runtime.telemetry.consumer_data_lost": {
        "domain": "runtime",
        "concerns": ("operational", "error"),
        "ids": ("consumer_id",),
    },
    "runtime.debug_tracer_disabled": {
        "domain": "runtime",
        "concerns": ("operational", "error"),
        "ids": (),
    },
    "runtime.stream_event_dropped": {
        "domain": "runtime",
        "concerns": ("operational", "error"),
        "ids": ("spawn_id",),
    },
    # Usage domain.
    "usage.command.invoked": {"domain": "usage", "concerns": ("usage",), "ids": ()},
    "usage.model.selected": {"domain": "usage", "concerns": ("usage",), "ids": ("spawn_id",)},
    "usage.spawn.launched": {"domain": "usage", "concerns": ("usage",), "ids": ("spawn_id",)},
}


def utc_timestamp() -> str:
    """Return a UTC ISO-8601 timestamp using the envelope's Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def validate_event(domain: str, event: str, severity: str | None) -> None:
    """Validate producer-supplied event fields against the v1 contract."""
    if domain not in VALID_DOMAINS:
        raise ValueError(f"invalid telemetry domain: {domain}")
    if severity is not None and severity not in VALID_SEVERITIES:
        raise ValueError(f"invalid telemetry severity: {severity}")
    definition = EVENT_REGISTRY.get(event)
    if definition is None:
        raise ValueError(f"unknown telemetry event: {event}")
    if definition["domain"] != domain:
        raise ValueError(
            f"telemetry event {event!r} belongs to domain {definition['domain']!r}, not {domain!r}"
        )


def concerns_for_event(event: str) -> tuple[Concern, ...]:
    """Return concern tags for a registered event."""
    return EVENT_REGISTRY[event]["concerns"]


def make_error_data(
    exc: BaseException | None = None, *, message: str | None = None
) -> dict[str, Any]:
    """Build structured error metadata for error-tagged events."""
    error_info: dict[str, Any] = {}
    if exc is not None:
        error_info["type"] = type(exc).__name__
        error_info["message"] = str(exc)
        error_info["stack"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    else:
        error_info["type"] = "UnknownError"
    if message is not None:
        error_info["message"] = message
    elif "message" not in error_info:
        error_info["message"] = "Unknown error"
    return {"error": error_info}
