"""Hook config loading and normalization."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from meridian.lib.config.layers import iter_config_layers
from meridian.lib.hooks.schema import hook_row_values, hooks_from_spec, normalize_hook_row
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


class HookConfigError(ValueError):
    """A syntactically readable config contains invalid hook rows."""


@dataclass(frozen=True)
class HookConfigLoad:
    """Valid hook rows plus every invalid row found while loading them."""

    config: HooksConfig
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def require_valid(self) -> HooksConfig:
        """Return the config or raise every invalid-row diagnostic."""

        if len(self.errors) == 1:
            raise HookConfigError(self.errors[0])
        if self.errors:
            details = "\n".join(f"- {error}" for error in self.errors)
            raise HookConfigError(f"Invalid hook rows:\n{details}")
        return self.config


def _hooks_from_payload(
    payload: dict[str, object], *, source: str
) -> tuple[tuple[Hook, ...], tuple[str, ...], tuple[str, ...]]:
    hooks: list[Hook] = []
    errors: list[str] = []
    warnings: list[str] = []

    raw_hooks = payload.get("hooks")
    if raw_hooks is None:
        return tuple(hooks), (), ()

    try:
        rows = hook_row_values(raw_hooks, source=f"{source}.hooks")
    except ValueError as exc:
        return (), (str(exc),), ()

    for index, raw_row in enumerate(rows, start=1):
        row_source = f"{source}.hooks[{index}]"
        try:
            spec = normalize_hook_row(raw_row, source=row_source)
            loaded = hooks_from_spec(
                spec,
                source=source,
                row_source=row_source,
            )
            hooks.extend(loaded.hooks)
            if loaded.warning is not None:
                warnings.append(loaded.warning)
        except ValueError as exc:
            errors.append(_named_row_error(raw_row, exc))
    return tuple(hooks), tuple(errors), tuple(warnings)


def _named_row_error(raw_row: object, exc: ValueError) -> str:
    """Lead with the row's own `name`, when it has one, so users find it."""

    if isinstance(raw_row, Mapping):
        name = cast("Mapping[str, object]", raw_row).get("name")
        if isinstance(name, str) and name.strip():
            return f"hook {name.strip()!r}: {exc}"
    return str(exc)


def _apply_name_overrides(hooks: tuple[Hook, ...]) -> tuple[Hook, ...]:
    """A later row with the same name and event replaces an earlier one."""

    effective: OrderedDict[tuple[str, HookEventName], Hook] = OrderedDict()
    for hook in hooks:
        key = (hook.name, hook.event)
        effective.pop(key, None)
        effective[key] = hook
    return tuple(effective.values())


def load_hooks_config_report(
    project_root: Path,
    *,
    user_config: Path | None = None,
    authority: RuntimeAuthoritySnapshot | None = None,
) -> HookConfigLoad:
    """Load valid rows with precedence and report every invalid row."""

    hooks: list[Hook] = list(_DEFAULT_HOOKS)
    errors: list[str] = []
    warnings: list[str] = []
    for layer in iter_config_layers(
        project_root,
        authority=authority,
        user_config=user_config,
    ):
        layer_hooks, layer_errors, layer_warnings = _hooks_from_payload(
            layer.payload,
            source=layer.name,
        )
        hooks.extend(layer_hooks)
        errors.extend(layer_errors)
        warnings.extend(layer_warnings)

    return HookConfigLoad(
        config=HooksConfig(hooks=_apply_name_overrides(tuple(hooks))),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def load_hooks_config(
    project_root: Path,
    *,
    user_config: Path | None = None,
    authority: RuntimeAuthoritySnapshot | None = None,
) -> HooksConfig:
    """Load hook config with precedence: default < user < project < local."""

    report = load_hooks_config_report(
        project_root,
        user_config=user_config,
        authority=authority,
    )
    return report.require_valid()
