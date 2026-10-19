"""Public hook exports loaded lazily to avoid import cycles."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meridian.lib.hooks.builtin import BUILTIN_HOOKS, BuiltinHook
    from meridian.lib.hooks.config import (
        HOOK_SOURCE_PRECEDENCE,
        HooksConfig,
        load_hooks_config,
    )
    from meridian.lib.hooks.dispatch import HookDispatcher
    from meridian.lib.hooks.registry import HookRegistry
    from meridian.lib.hooks.types import (
        EVENTS,
        HOOK_CONTEXT_SCHEMA_VERSION,
        EventSpec,
        Hook,
        HookContext,
        HookEventName,
        HookOutcome,
        HookResult,
        HookWhen,
        HookWhenField,
        SpawnStatus,
    )

__all__ = [
    "BUILTIN_HOOKS",
    "EVENTS",
    "HOOK_CONTEXT_SCHEMA_VERSION",
    "HOOK_SOURCE_PRECEDENCE",
    "BuiltinHook",
    "EventSpec",
    "Hook",
    "HookContext",
    "HookDispatcher",
    "HookEventName",
    "HookOutcome",
    "HookRegistry",
    "HookResult",
    "HookWhen",
    "HookWhenField",
    "HooksConfig",
    "SpawnStatus",
    "load_hooks_config",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "BUILTIN_HOOKS": ("meridian.lib.hooks.builtin", "BUILTIN_HOOKS"),
    "BuiltinHook": ("meridian.lib.hooks.builtin", "BuiltinHook"),
    "HOOK_SOURCE_PRECEDENCE": ("meridian.lib.hooks.config", "HOOK_SOURCE_PRECEDENCE"),
    "HooksConfig": ("meridian.lib.hooks.config", "HooksConfig"),
    "load_hooks_config": ("meridian.lib.hooks.config", "load_hooks_config"),
    "HookDispatcher": ("meridian.lib.hooks.dispatch", "HookDispatcher"),
    "HookRegistry": ("meridian.lib.hooks.registry", "HookRegistry"),
    "EVENTS": ("meridian.lib.hooks.types", "EVENTS"),
    "HOOK_CONTEXT_SCHEMA_VERSION": (
        "meridian.lib.hooks.types",
        "HOOK_CONTEXT_SCHEMA_VERSION",
    ),
    "EventSpec": ("meridian.lib.hooks.types", "EventSpec"),
    "Hook": ("meridian.lib.hooks.types", "Hook"),
    "HookContext": ("meridian.lib.hooks.types", "HookContext"),
    "HookEventName": ("meridian.lib.hooks.types", "HookEventName"),
    "HookOutcome": ("meridian.lib.hooks.types", "HookOutcome"),
    "HookResult": ("meridian.lib.hooks.types", "HookResult"),
    "HookWhen": ("meridian.lib.hooks.types", "HookWhen"),
    "HookWhenField": ("meridian.lib.hooks.types", "HookWhenField"),
    "SpawnStatus": ("meridian.lib.hooks.types", "SpawnStatus"),
}


def __getattr__(name: str) -> Any:
    module_attr = _EXPORTS.get(name)
    if module_attr is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = module_attr
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
