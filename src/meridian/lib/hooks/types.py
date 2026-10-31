"""Hook contracts shared across registration, dispatch, and execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

# Context, result and event names are the plugin API's own types: built-in and
# external hooks see exactly what the dispatcher builds.
from meridian.plugin_api.types import (
    HOOK_CONTEXT_SCHEMA_VERSION,
    NOTIFICATION_EVENTS,
    HookContext,
    HookEventName,
    HookOutcome,
    HookResult,
    Notification,
    SpawnKind,
    SpawnStatus,
)

HookWhenField = Literal["status", "agent", "kind"]


@dataclass(frozen=True)
class EventSpec:
    """Runtime facts supplied by one hook event."""

    default_timeout_secs: int
    when_fields: frozenset[HookWhenField]


_SPAWN_FIELDS: frozenset[HookWhenField] = frozenset(("agent", "kind"))
_FINISHED_SPAWN_FIELDS: frozenset[HookWhenField] = frozenset(("status", "agent", "kind"))
_WORK_FIELDS: frozenset[HookWhenField] = frozenset()

EVENTS: dict[HookEventName, EventSpec] = {
    "spawn.created": EventSpec(30, _SPAWN_FIELDS),
    "spawn.started": EventSpec(30, _SPAWN_FIELDS),
    "spawn.finished": EventSpec(60, _FINISHED_SPAWN_FIELDS),
    "work.created": EventSpec(30, _WORK_FIELDS),
    "work.activated": EventSpec(30, _WORK_FIELDS),
    "work.done": EventSpec(60, _WORK_FIELDS),
    "notify.your_turn": EventSpec(30, _SPAWN_FIELDS),
    "notify.cache_expiring": EventSpec(30, _SPAWN_FIELDS),
    "notify.needs_permission": EventSpec(30, _SPAWN_FIELDS),
    "notify.manual": EventSpec(30, _SPAWN_FIELDS),
}


def _default_options() -> dict[str, object]:
    return {}


@dataclass(frozen=True)
class HookWhen:
    """Conditional execution filters."""

    status: tuple[SpawnStatus, ...] | None = None
    agent: str | None = None
    kind: tuple[SpawnKind, ...] | None = None


@dataclass(frozen=True)
class Hook:
    """Registered hook configuration."""

    name: str
    event: HookEventName
    source: str
    command: str | None = None
    builtin: str | None = None
    timeout_secs: int | None = None
    interval: str | None = None
    enabled: bool = True
    priority: int = 0
    require_serial: bool = False
    when: HookWhen | None = None
    exclude: tuple[str, ...] = ()
    options: Mapping[str, object] = field(default_factory=_default_options)
    # Legacy field kept for backward compatibility.
    # Builtins should prefer config.options.
    remote: str | None = None


__all__ = [
    "EVENTS",
    "HOOK_CONTEXT_SCHEMA_VERSION",
    "NOTIFICATION_EVENTS",
    "EventSpec",
    "Hook",
    "HookContext",
    "HookEventName",
    "HookOutcome",
    "HookResult",
    "HookWhen",
    "HookWhenField",
    "Notification",
    "SpawnKind",
    "SpawnStatus",
]
