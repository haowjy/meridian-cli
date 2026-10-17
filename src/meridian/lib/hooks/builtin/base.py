"""Builtin hook protocol contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from meridian.plugin_api import Hook, HookContext, HookEventName, HookResult

if TYPE_CHECKING:
    from pydantic import BaseModel


class BuiltinHook(Protocol):
    """Protocol implemented by in-process built-in hooks; each owns its metadata."""

    name: str
    requirements: tuple[str, ...]
    # Events an unevented row runs on, and its throttle when the row sets none.
    default_events: tuple[HookEventName, ...]
    default_interval: str | None
    # Validates a row's `options` when config loads.
    options_model: type[BaseModel]
    # Options that make two unnamed rows different targets (and different names).
    identity_options: tuple[str, ...]

    def check_requirements(self) -> tuple[bool, str | None]:
        """Return whether runtime requirements are available."""
        ...

    def execute(self, context: HookContext, config: Hook) -> HookResult:
        """Execute one hook invocation."""
        ...
