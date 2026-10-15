"""Hook contracts shared across registration, dispatch, and execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from meridian.plugin_api.types import HookContext as PluginHookContext

HookContext = PluginHookContext

HookEventName = Literal[
    "spawn.created",
    "spawn.running",
    "spawn.start",
    "spawn.finalized",
    "work.start",
    "work.started",
    "work.done",
]

HookEventClass = Literal["observe", "post", "gate"]

SpawnStatus = Literal["success", "failure", "cancelled", "timeout", "skipped"]
FailurePolicy = Literal["fail", "warn", "ignore"]
HookOutcome = Literal["success", "failure", "timeout", "skipped"]

EVENT_CLASS: dict[HookEventName, HookEventClass] = {
    "spawn.created": "observe",
    "spawn.running": "observe",
    "spawn.start": "observe",
    "spawn.finalized": "post",
    "work.start": "observe",
    "work.started": "observe",
    "work.done": "post",
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

HOOK_CONTEXT_SCHEMA_VERSION = 1


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


@dataclass
class HookResult:
    """Result for one hook execution."""

    hook_name: str
    event: HookEventName
    outcome: HookOutcome
    success: bool
    skipped: bool = False
    skip_reason: str | None = None
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: str | None = None
    stderr: str | None = None
