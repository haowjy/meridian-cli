from __future__ import annotations

import os
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
    # A machine event belongs to no project: its rows are read from the user config
    # alone and run with no project root, so a repository can neither add nor replace one.
    machine: bool = False


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
    "inbox.received": EventSpec(60, frozenset(), machine=True),
}

# The one config layer a machine event's rows may come from.
MACHINE_EVENT_SOURCE = "user"


def hooks_dispatch_enabled(env: Mapping[str, str] | None = None) -> bool:
    scope = os.environ if env is None else env
    value = scope.get("MERIDIAN_HOOKS_ENABLED")
    if value is None:
        return True
    return value.strip().lower() != "false"


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
    options: Mapping[str, object] = field(default_factory=_default_options)


__all__ = [
    "EVENTS",
    "HOOK_CONTEXT_SCHEMA_VERSION",
    "MACHINE_EVENT_SOURCE",
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
    "hooks_dispatch_enabled",
]
