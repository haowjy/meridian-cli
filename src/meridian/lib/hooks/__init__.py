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
        DEFAULT_FAILURE_POLICY,
        DEFAULT_TIMEOUTS,
        EVENT_CLASS,
        HOOK_CONTEXT_SCHEMA_VERSION,
        FailurePolicy,
        Hook,
        HookContext,
        HookEventClass,
        HookEventName,
        HookOutcome,
        HookResult,
        HookWhen,
        SpawnStatus,
    )

__all__ = [
    "BUILTIN_HOOKS",
    "DEFAULT_FAILURE_POLICY",
    "DEFAULT_TIMEOUTS",
    "EVENT_CLASS",
    "HOOK_CONTEXT_SCHEMA_VERSION",
    "HOOK_SOURCE_PRECEDENCE",
    "BuiltinHook",
    "FailurePolicy",
    "Hook",
    "HookContext",
    "HookDispatcher",
    "HookEventClass",
    "HookEventName",
    "HookOutcome",
    "HookRegistry",
    "HookResult",
    "HookWhen",
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
    "DEFAULT_FAILURE_POLICY": ("meridian.lib.hooks.types", "DEFAULT_FAILURE_POLICY"),
    "DEFAULT_TIMEOUTS": ("meridian.lib.hooks.types", "DEFAULT_TIMEOUTS"),
    "EVENT_CLASS": ("meridian.lib.hooks.types", "EVENT_CLASS"),
    "HOOK_CONTEXT_SCHEMA_VERSION": (
        "meridian.lib.hooks.types",
        "HOOK_CONTEXT_SCHEMA_VERSION",
    ),
    "FailurePolicy": ("meridian.lib.hooks.types", "FailurePolicy"),
    "Hook": ("meridian.lib.hooks.types", "Hook"),
    "HookContext": ("meridian.lib.hooks.types", "HookContext"),
    "HookEventClass": ("meridian.lib.hooks.types", "HookEventClass"),
    "HookEventName": ("meridian.lib.hooks.types", "HookEventName"),
    "HookOutcome": ("meridian.lib.hooks.types", "HookOutcome"),
    "HookResult": ("meridian.lib.hooks.types", "HookResult"),
    "HookWhen": ("meridian.lib.hooks.types", "HookWhen"),
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
