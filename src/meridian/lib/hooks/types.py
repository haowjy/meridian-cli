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
    FailurePolicy,
    HookContext,
    HookEventName,
    HookOutcome,
    HookResult,
    Notification,
    SpawnStatus,
)

HookEventClass = Literal["observe", "post", "gate"]


EVENT_CLASS: dict[HookEventName, HookEventClass] = {
    "spawn.created": "observe",
    "spawn.running": "observe",
    "spawn.start": "observe",
    "spawn.finalized": "post",
    "work.start": "observe",
    "work.started": "observe",
    "work.done": "post",
    "session.responded": "observe",
    "session.cache_expiring": "observe",
    "notify.manual": "observe",
}

DEFAULT_TIMEOUTS: dict[HookEventClass, int] = {
    "observe": 30,
    "post": 60,
    "gate": 1,
}

DEFAULT_FAILURE_POLICY: dict[HookEventClass, FailurePolicy] = {
    "observe": "warn",
    "post": "warn",
    "gate": "fail",
}

def _default_options() -> dict[str, object]:
    return {}


@dataclass(frozen=True)
class HookWhen:
    """Conditional execution filters."""

    status: tuple[SpawnStatus, ...] | None = None
    agent: str | None = None


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
    failure_policy: FailurePolicy | None = None
    require_serial: bool = False
    when: HookWhen | None = None
    exclude: tuple[str, ...] = ()
    options: Mapping[str, object] = field(default_factory=_default_options)
    auto_registered: bool = False
    # Legacy field kept for backward compatibility.
    # Builtins should prefer config.options.
    remote: str | None = None


__all__ = [
    "DEFAULT_FAILURE_POLICY",
    "DEFAULT_TIMEOUTS",
    "EVENT_CLASS",
    "HOOK_CONTEXT_SCHEMA_VERSION",
    "NOTIFICATION_EVENTS",
    "FailurePolicy",
    "Hook",
    "HookContext",
    "HookEventClass",
    "HookEventName",
    "HookOutcome",
    "HookResult",
    "HookWhen",
    "Notification",
    "SpawnStatus",
]
