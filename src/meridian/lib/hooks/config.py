"""Hook config loading and normalization."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.config.layers import iter_config_layers
from meridian.lib.hooks.schema import hooks_from_spec, normalize_hooks_array
from meridian.lib.hooks.types import NOTIFICATION_EVENTS, Hook, HookEventName

if TYPE_CHECKING:
    from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot

HOOK_SOURCE_PRECEDENCE: tuple[str, ...] = ("user", "project", "local")
# Source of the routes Meridian supplies itself.
DEFAULT_SOURCE = "default"


@dataclass(frozen=True)
class HooksConfig:
    """Resolved hook config after source layering and override semantics."""

    hooks: tuple[Hook, ...]


def _hooks_from_payload(payload: dict[str, object], *, source: str) -> tuple[Hook, ...]:
    hooks: list[Hook] = []

    raw_hooks = payload.get("hooks")
    if raw_hooks is None:
        return tuple(hooks)

    specs = normalize_hooks_array(raw_hooks, source=f"{source}.hooks")
    for index, spec in enumerate(specs, start=1):
        hooks.extend(
            hooks_from_spec(
                spec,
                source=source,
                row_source=f"{source}.hooks[{index}]",
            )
        )
    return tuple(hooks)


def _apply_name_overrides(hooks: tuple[Hook, ...]) -> tuple[Hook, ...]:
    effective: OrderedDict[tuple[str, HookEventName | None], Hook] = OrderedDict()
    for hook in hooks:
        if hook.builtin is not None and not hook.auto_registered:
            to_remove = [
                key
                for key, existing in effective.items()
                if existing.auto_registered and existing.builtin == hook.builtin
            ]
            for key in to_remove:
                del effective[key]

        if hook.auto_registered and hook.builtin is not None:
            key = (hook.name, hook.event)
            if key in effective:
                del effective[key]
            effective[key] = hook
            continue

        # For non-auto-registered hooks, key by (name, event) to allow multi-event builtins
        # Still remove previous hooks with same name AND event (override semantics)
        key = (hook.name, hook.event)
        if key in effective:
            del effective[key]
        effective[key] = hook
    return tuple(effective.values())


def load_hooks_config(
    project_root: Path,
    *,
    user_config: Path | None = None,
    authority: RuntimeAuthoritySnapshot | None = None,
) -> HooksConfig:
    """Load hook config with precedence: user < project < local."""

    hooks: list[Hook] = []
    for layer in iter_config_layers(
        project_root,
        authority=authority,
        user_config=user_config,
    ):
        hooks.extend(_hooks_from_payload(layer.payload, source=layer.name))

    return HooksConfig(hooks=_with_default_notify_routes(_apply_name_overrides(tuple(hooks))))


def _with_default_notify_routes(hooks: tuple[Hook, ...]) -> tuple[Hook, ...]:
    """Deliver every notification event that no `notify` row routes yet.

    A row naming `builtin = "notify"` for an event, enabled or not, replaces the
    default route for that event only.
    """

    routed = {hook.event for hook in hooks if hook.builtin == "notify"}
    defaults = tuple(
        Hook(
            name="notify",
            event=event,
            source=DEFAULT_SOURCE,
            builtin="notify",
            auto_registered=True,
        )
        for event in NOTIFICATION_EVENTS
        if event not in routed
    )
    return (*hooks, *defaults)
