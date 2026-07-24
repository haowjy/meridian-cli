"""Harness event normalization through per-bundle semantic ports."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import RawHarnessEvent


ActivityState = Literal["turn_active", "idle"]
MERIDIAN_CONNECTION_CLOSED_EVENT = "meridian/error/connectionClosed"


class TerminalOutcomeCause(StrEnum):
    """Typed cause used only when completion policy may refine an outcome."""

    REPLACEABLE_TRANSPORT_CLOSE = "replaceable_transport_close"


@dataclass(frozen=True)
class TerminalEventOutcome:
    status: SpawnStatus
    exit_code: int
    error: str | None = None
    cause: TerminalOutcomeCause | None = None

    def __post_init__(self) -> None:
        if self.status == SpawnStatus.SUCCEEDED and (
            self.exit_code != 0 or self.error is not None
        ):
            raise ValueError("succeeded terminal outcomes require exit_code=0 and no error")


@dataclass(frozen=True)
class PrimaryEventScope:
    """Primary harness event scope owned by a live connection."""

    harness_id: HarnessId
    scope_id: str
    unscoped_events_match: bool = False


@dataclass(frozen=True)
class EventSemantics:
    """All semantic meaning derived from one raw harness event."""

    activity: ActivityState | None = None
    clears_signal: bool = False
    terminal: TerminalEventOutcome | None = None


@dataclass(frozen=True)
class NormalizedHarnessEvent:
    """Raw evidence carried with its single semantic interpretation."""

    raw: RawHarnessEvent
    semantics: EventSemantics


type PayloadSemanticResolver = Callable[["RawHarnessEvent"], TerminalEventOutcome | None]
type ScopeIdResolver = Callable[[dict[str, object]], str | None]


def stringify_terminal_error(error: object) -> str | None:
    """Render an arbitrary upstream error payload for durable state."""

    if error is None:
        return None
    if isinstance(error, str):
        normalized = error.strip()
        return normalized or None
    try:
        rendered = json.dumps(error, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(error)
    normalized = rendered.strip()
    return normalized or None


def connection_closed_outcome(
    event: RawHarnessEvent,
    *,
    cause: TerminalOutcomeCause | None = None,
) -> TerminalEventOutcome:
    """Build the shared outcome for a Meridian synthetic connection close."""

    error = stringify_terminal_error(event.payload.get("message")) or "connection_closed"
    backend_exit_code = event.payload.get("backend_exit_code")
    stderr_excerpt = stringify_terminal_error(
        event.payload.get("backend_stderr_excerpt")
    )
    diagnostics: list[str] = []
    if isinstance(backend_exit_code, int):
        diagnostics.append(f"backend exit code: {backend_exit_code}")
    if stderr_excerpt and stderr_excerpt not in error:
        diagnostics.append(f"backend stderr:\n{stderr_excerpt}")
    if diagnostics:
        error = f"{error}\n\n" + "\n\n".join(diagnostics)
    return TerminalEventOutcome(status=SpawnStatus.FAILED, exit_code=1, error=error, cause=cause)


@dataclass(frozen=True)
class HarnessSemantics:
    """Per-harness port from open raw events to one typed semantic descriptor."""

    events: Mapping[str, EventSemantics]
    payload_resolvers: Mapping[str, PayloadSemanticResolver] = MappingProxyType({})
    scoped_events: frozenset[str] = frozenset()
    scope_id_resolver: ScopeIdResolver | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", MappingProxyType(dict(self.events)))
        object.__setattr__(
            self,
            "payload_resolvers",
            MappingProxyType(dict(self.payload_resolvers)),
        )
        if not self.payload_resolvers.keys() <= self.events.keys():
            raise ValueError("payload resolver events must be declared in events")
        if not self.scoped_events.issubset(self.events):
            raise ValueError("scoped events must be declared in events")
        if self.scoped_events and self.scope_id_resolver is None:
            raise ValueError("scoped semantic events require a scope_id_resolver")

    def normalize(
        self,
        event: RawHarnessEvent,
        *,
        primary_event_scope: PrimaryEventScope | None = None,
    ) -> EventSemantics:
        """Normalize one event; unknown or out-of-scope events have no semantics."""

        descriptor = self.events.get(event.event_type)
        if descriptor is None or not self._matches_scope(event, primary_event_scope):
            return EventSemantics()
        resolver = self.payload_resolvers.get(event.event_type)
        if resolver is None:
            return descriptor
        return EventSemantics(
            activity=descriptor.activity,
            clears_signal=descriptor.clears_signal,
            terminal=resolver(event),
        )

    def _matches_scope(
        self,
        event: RawHarnessEvent,
        primary_event_scope: PrimaryEventScope | None,
    ) -> bool:
        if event.event_type not in self.scoped_events or primary_event_scope is None:
            return True
        if event.harness_id != primary_event_scope.harness_id.value:
            return True
        assert self.scope_id_resolver is not None
        event_scope_id = self.scope_id_resolver(event.payload)
        if event_scope_id is None:
            return primary_event_scope.unscoped_events_match
        return event_scope_id == primary_event_scope.scope_id


def normalize_event(
    event: RawHarnessEvent,
    *,
    primary_event_scope: PrimaryEventScope | None = None,
) -> NormalizedHarnessEvent:
    """Dispatch by harness identity before interpreting the raw event name."""

    try:
        harness_id = HarnessId(event.harness_id)
    except ValueError:
        return NormalizedHarnessEvent(event, EventSemantics())

    # Lazy imports preserve the load-bearing adapter bootstrap order.
    from meridian.lib.harness import ensure_bootstrap
    from meridian.lib.harness.bundle import get_harness_bundle

    ensure_bootstrap()
    semantics = get_harness_bundle(harness_id).semantics.normalize(
        event,
        primary_event_scope=primary_event_scope,
    )
    return NormalizedHarnessEvent(event, semantics)


__all__ = [
    "MERIDIAN_CONNECTION_CLOSED_EVENT",
    "ActivityState",
    "EventSemantics",
    "HarnessSemantics",
    "NormalizedHarnessEvent",
    "PrimaryEventScope",
    "TerminalEventOutcome",
    "TerminalOutcomeCause",
    "connection_closed_outcome",
    "normalize_event",
    "stringify_terminal_error",
]
