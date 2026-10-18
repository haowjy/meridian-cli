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

# Meridian's own rows rank first, so any config layer overrides them.
DEFAULT_SOURCE = "default"
HOOK_SOURCE_PRECEDENCE: tuple[str, ...] = (DEFAULT_SOURCE, "user", "project", "local")

# One `notify` route per notification event; a config row of the same name and
# event (an unnamed `builtin = "notify"` row is named `notify`) replaces it.
_DEFAULT_HOOKS: tuple[Hook, ...] = tuple(
    Hook(name="notify", event=event, source=DEFAULT_SOURCE, builtin="notify")
    for event in NOTIFICATION_EVENTS
)


@dataclass(frozen=True)
class HooksConfig:
    """Resolved hook config after source layering and override semantics."""

    hooks: tuple[Hook, ...]


def default_hooks_config() -> HooksConfig:
    """Return Meridian's built-in lowest-precedence hook layer."""

    return HooksConfig(hooks=_DEFAULT_HOOKS)


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
    """A later row with the same name and event replaces an earlier one."""

    effective: OrderedDict[tuple[str, HookEventName], Hook] = OrderedDict()
    for hook in hooks:
        key = (hook.name, hook.event)
        effective.pop(key, None)
        effective[key] = hook
    return tuple(effective.values())


def load_hooks_config(
    project_root: Path,
    *,
    user_config: Path | None = None,
    authority: RuntimeAuthoritySnapshot | None = None,
) -> HooksConfig:
    """Load hook config with precedence: default < user < project < local."""

    hooks: list[Hook] = list(_DEFAULT_HOOKS)
    for layer in iter_config_layers(
        project_root,
        authority=authority,
        user_config=user_config,
    ):
        hooks.extend(_hooks_from_payload(layer.payload, source=layer.name))

    return HooksConfig(hooks=_apply_name_overrides(tuple(hooks)))
