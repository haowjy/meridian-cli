"""Harness event normalization through per-bundle semantic ports."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class PrimaryEventScope:
    """Primary harness event scope for parent-session classification."""

    harness_id: HarnessId
    scope_id: str
    unscoped_events_match: bool = False


@dataclass(frozen=True)
class ActivitySemanticEvent:
    """Closed semantic variant describing UI activity."""

    kind: Literal["activity"] = field(default="activity", init=False)
    state: ActivityState = "idle"


@dataclass(frozen=True)
class TerminalSemanticEvent:
    """Closed semantic variant describing a terminal outcome."""

    outcome: TerminalEventOutcome
    kind: Literal["terminal"] = field(default="terminal", init=False)


@dataclass(frozen=True)
class SignalClearedSemanticEvent:
    """Closed semantic variant describing acknowledgement of a user signal."""

    kind: Literal["signal_cleared"] = field(default="signal_cleared", init=False)


type SemanticEvent = ActivitySemanticEvent | TerminalSemanticEvent | SignalClearedSemanticEvent


class SemanticClass(StrEnum):
    """Declarative semantic classes registered by a harness bundle."""

    TURN_ACTIVE = "turn_active"
    IDLE = "idle"
    SIGNAL_CLEARED = "signal_cleared"
    TERMINAL_SUCCESS = "terminal_success"
    TERMINAL_PAYLOAD = "terminal_payload"


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
    return TerminalEventOutcome(status=SpawnStatus.FAILED, exit_code=1, error=error, cause=cause)


def codex_primary_event_scope(thread_id: str | None) -> PrimaryEventScope | None:
    """Build Codex's primary-thread scope from a transport observation."""

    normalized = (thread_id or "").strip()
    if not normalized:
        return None
    return PrimaryEventScope(
        harness_id=HarnessId.CODEX,
        scope_id=normalized,
        unscoped_events_match=True,
    )


def opencode_primary_event_scope(session_id: str | None) -> PrimaryEventScope | None:
    """Build OpenCode's primary-session scope from its launch response."""

    normalized = (session_id or "").strip()
    if not normalized:
        return None
    return PrimaryEventScope(harness_id=HarnessId.OPENCODE, scope_id=normalized)


@dataclass(frozen=True)
class HarnessSemantics:
    """Per-harness port from open raw events to the closed semantic union."""

    event_classes: Mapping[str, frozenset[SemanticClass]]
    payload_resolver: PayloadSemanticResolver | None = None
    scoped_events: frozenset[str] = frozenset()
    scope_id_resolver: ScopeIdResolver | None = None
    primary_scope_event: str | None = None
    primary_scope_unscoped_events_match: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_classes",
            MappingProxyType(dict(self.event_classes)),
        )
        if any(not classes for classes in self.event_classes.values()):
            raise ValueError("HarnessSemantics event classes must not be empty")
        if (
            any(
                SemanticClass.TERMINAL_PAYLOAD in classes for classes in self.event_classes.values()
            )
            and self.payload_resolver is None
        ):
            raise ValueError("terminal_payload event requires a payload_resolver")
        if not self.scoped_events.issubset(self.event_classes):
            raise ValueError("scoped events must be declared in event_classes")
        if self.scoped_events and self.scope_id_resolver is None:
            raise ValueError("scoped semantic events require a scope_id_resolver")
        if self.primary_scope_event is not None and self.scope_id_resolver is None:
            raise ValueError("primary scope observation requires a scope_id_resolver")
        if (
            self.primary_scope_event is not None
            and self.primary_scope_event not in self.event_classes
        ):
            raise ValueError("primary scope event must be declared in event_classes")

    def normalize(
        self,
        event: RawHarnessEvent,
        *,
        primary_event_scope: PrimaryEventScope | None = None,
    ) -> tuple[SemanticEvent, ...]:
        """Normalize one known event; unknown upstream names remain ignorable."""

        classes = self.event_classes.get(event.event_type)
        if classes is None or not self._matches_scope(event, primary_event_scope):
            return ()

        normalized: list[SemanticEvent] = []
        if SemanticClass.TURN_ACTIVE in classes:
            normalized.append(ActivitySemanticEvent(state="turn_active"))
        if SemanticClass.IDLE in classes:
            normalized.append(ActivitySemanticEvent(state="idle"))
        if SemanticClass.SIGNAL_CLEARED in classes:
            normalized.append(SignalClearedSemanticEvent())
        if SemanticClass.TERMINAL_SUCCESS in classes:
            normalized.append(
                TerminalSemanticEvent(
                    TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0)
                )
            )
        if SemanticClass.TERMINAL_PAYLOAD in classes:
            if self.payload_resolver is None:
                raise RuntimeError("terminal_payload event has no payload resolver")
            outcome = self.payload_resolver(event)
            if outcome is not None:
                normalized.append(TerminalSemanticEvent(outcome))
        return tuple(normalized)

    def observe_primary_scope(self, event: RawHarnessEvent) -> PrimaryEventScope | None:
        if event.event_type != self.primary_scope_event or self.scope_id_resolver is None:
            return None
        try:
            harness_id = HarnessId(event.harness_id)
        except ValueError:
            return None
        scope_id = self.scope_id_resolver(event.payload)
        if scope_id is None:
            return None
        return PrimaryEventScope(
            harness_id=harness_id,
            scope_id=scope_id,
            unscoped_events_match=self.primary_scope_unscoped_events_match,
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
) -> tuple[SemanticEvent, ...]:
    """Dispatch by harness identity before interpreting the raw event name."""

    try:
        harness_id = HarnessId(event.harness_id)
    except ValueError:
        return ()

    # Lazy imports preserve the load-bearing adapter bootstrap order.
    from meridian.lib.harness import ensure_bootstrap
    from meridian.lib.harness.bundle import get_harness_bundle

    ensure_bootstrap()
    return get_harness_bundle(harness_id).semantics.normalize(
        event,
        primary_event_scope=primary_event_scope,
    )


def terminal_outcome(
    event: RawHarnessEvent,
    *,
    primary_event_scope: PrimaryEventScope | None = None,
) -> TerminalEventOutcome | None:
    for semantic in normalize_event(event, primary_event_scope=primary_event_scope):
        if isinstance(semantic, TerminalSemanticEvent):
            return semantic.outcome
    return None


def activity_transition(
    event: RawHarnessEvent,
    *,
    primary_event_scope: PrimaryEventScope | None = None,
) -> ActivityState | None:
    for semantic in normalize_event(event, primary_event_scope=primary_event_scope):
        if isinstance(semantic, ActivitySemanticEvent):
            return semantic.state
    return None


def clears_signal(
    event: RawHarnessEvent,
    *,
    primary_event_scope: PrimaryEventScope | None = None,
) -> bool:
    return any(
        isinstance(semantic, SignalClearedSemanticEvent)
        for semantic in normalize_event(event, primary_event_scope=primary_event_scope)
    )


@dataclass
class PrimaryEventScopeTracker:
    """Track the primary scope declared by the event's harness bundle."""

    primary_event_scope: PrimaryEventScope | None = field(default=None)

    def observe(self, event: RawHarnessEvent) -> None:
        if self.primary_event_scope is not None:
            return
        try:
            harness_id = HarnessId(event.harness_id)
        except ValueError:
            return
        from meridian.lib.harness import ensure_bootstrap
        from meridian.lib.harness.bundle import get_harness_bundle

        ensure_bootstrap()
        self.primary_event_scope = get_harness_bundle(harness_id).semantics.observe_primary_scope(
            event
        )

    def terminal_outcome(self, event: RawHarnessEvent) -> TerminalEventOutcome | None:
        self.observe(event)
        return terminal_outcome(event, primary_event_scope=self.primary_event_scope)

    def activity_transition(self, event: RawHarnessEvent) -> ActivityState | None:
        self.observe(event)
        return activity_transition(event, primary_event_scope=self.primary_event_scope)


__all__ = [
    "MERIDIAN_CONNECTION_CLOSED_EVENT",
    "ActivitySemanticEvent",
    "ActivityState",
    "HarnessSemantics",
    "PrimaryEventScope",
    "PrimaryEventScopeTracker",
    "SemanticClass",
    "SemanticEvent",
    "SignalClearedSemanticEvent",
    "TerminalEventOutcome",
    "TerminalOutcomeCause",
    "TerminalSemanticEvent",
    "activity_transition",
    "clears_signal",
    "codex_primary_event_scope",
    "connection_closed_outcome",
    "normalize_event",
    "opencode_primary_event_scope",
    "stringify_terminal_error",
    "terminal_outcome",
]
