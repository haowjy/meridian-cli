"""Hook config loading and normalization."""

from __future__ import annotations

import re
import tomllib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast, get_args

from meridian.lib.config.project_config_state import resolve_project_config_state
from meridian.lib.config.project_paths import resolve_project_config_paths
from meridian.lib.config.project_root import resolve_user_config_path
from meridian.lib.config.settings import normalize_hooks_array
from meridian.lib.hooks.builtin_registry import (
    BUILTIN_HOOK_REGISTRY,
    get_default_events,
    validate_builtin_options,
)
from meridian.lib.hooks.types import (
    EVENT_CLASS,
    FailurePolicy,
    Hook,
    HookEventName,
    HookWhen,
    SpawnStatus,
)

if TYPE_CHECKING:
    from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot

HOOK_SOURCE_PRECEDENCE: tuple[str, ...] = ("builtin", "context", "user", "project", "local")
_LOCAL_CONFIG_FILENAME = "meridian.local.toml"
_INTERVAL_PATTERN = re.compile(r"^\d+[smhd]$")
_KNOWN_FAILURE_POLICIES = frozenset({"fail", "warn", "ignore"})
_KNOWN_SPAWN_STATUSES = frozenset(get_args(SpawnStatus))

# Backward-compatible alias; registry remains the single source of truth.
BUILTIN_HOOK_DEFAULTS = BUILTIN_HOOK_REGISTRY


@dataclass(frozen=True)
class HooksConfig:
    """Resolved hook config after source layering and override semantics."""

    hooks: tuple[Hook, ...]


@dataclass(frozen=True)
class HookConfigPaths:
    """Shared authority paths for hook config source layering."""

    project_root: Path
    user_config: Path | None
    project_config: Path | None
    local_config: Path


def _resolve_hook_config_paths(
    project_root: Path,
    *,
    user_config: Path | None = None,
    authority: RuntimeAuthoritySnapshot | None = None,
) -> HookConfigPaths:
    if authority is not None:
        resolved_project_root = authority.project_root.expanduser().resolve()
        project_paths = authority.project_config_paths
    else:
        resolved_project_root = project_root.expanduser().resolve()
        project_paths = resolve_project_config_paths(resolved_project_root)

    resolved_user_config = resolve_user_config_path(user_config)
    project_config = resolve_project_config_state(
        resolved_project_root,
        project_paths=project_paths,
    ).path
    return HookConfigPaths(
        project_root=resolved_project_root,
        user_config=resolved_user_config,
        project_config=project_config,
        local_config=resolved_project_root / _LOCAL_CONFIG_FILENAME,
    )


def _read_toml(path: Path) -> dict[str, object]:
    payload_obj = tomllib.loads(path.read_text(encoding="utf-8"))
    return cast("dict[str, object]", payload_obj)


def _parse_event(raw: str, *, source: str) -> HookEventName:
    if raw not in EVENT_CLASS:
        valid = ", ".join(sorted(EVENT_CLASS.keys()))
        raise ValueError(f"Invalid value for '{source}': expected one of [{valid}], got {raw!r}.")
    return raw


def _parse_interval(raw: str | None, *, source: str) -> str | None:
    if raw is None:
        return None
    if not _INTERVAL_PATTERN.fullmatch(raw):
        raise ValueError(
            f"Invalid value for '{source}': expected interval format '\\d+[smhd]', got {raw!r}."
        )
    return raw


def _parse_when(raw: object, *, source: str) -> HookWhen | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid value for '{source}': expected table.")

    raw_dict = cast("dict[str, object]", raw)

    status_values: tuple[str, ...] | None = None
    status = raw_dict.get("status")
    if status is not None:
        if not isinstance(status, tuple):
            raise ValueError(f"Invalid value for '{source}.status': expected array[str].")
        for value in cast("tuple[object, ...]", status):
            if not isinstance(value, str):
                raise ValueError(f"Invalid value for '{source}.status': expected array[str].")
            if value not in _KNOWN_SPAWN_STATUSES:
                valid = ", ".join(sorted(_KNOWN_SPAWN_STATUSES))
                raise ValueError(
                    f"Invalid value for '{source}.status': expected values in [{valid}], "
                    f"got {value!r}."
                )
        status_values = cast("tuple[str, ...]", status)

    agent_obj = raw_dict.get("agent")
    if agent_obj is not None and not isinstance(agent_obj, str):
        raise ValueError(
            f"Invalid value for '{source}.agent': expected str, got "
            f"{type(agent_obj).__name__} ({agent_obj!r})."
        )
    agent = agent_obj

    if status_values is None and agent is None:
        return None
    return HookWhen(
        status=cast("tuple[SpawnStatus, ...] | None", status_values),
        agent=agent,
    )


def _hook_from_row(
    row: dict[str, object],
    *,
    source: str,
    row_source: str,
    auto_registered: bool = False,
) -> tuple[Hook, ...]:
    command = cast("str | None", row.get("command"))
    builtin = cast("str | None", row.get("builtin"))

    raw_options = row.get("options")
    options: dict[str, object]
    if raw_options is None:
        options = {}
    elif isinstance(raw_options, dict):
        options = dict(cast("dict[str, object]", raw_options))
    else:
        raise ValueError(f"Invalid value for '{row_source}.options': expected table.")

    # Parse remote URL - primary field
    remote = cast("str | None", row.get("remote"))

    # Also check options.remote for plugin-style config
    if remote is not None:
        options.setdefault("remote", remote)

    if command is not None and builtin is not None:
        raise ValueError(
            f"Invalid hook config '{row_source}': 'command' and 'builtin' are mutually exclusive."
        )
    if command is None and builtin is None:
        raise ValueError(
            f"Invalid hook config '{row_source}': expected either 'command' or 'builtin'."
        )

    name = cast("str | None", row.get("name"))
    if name is None:
        if builtin is not None:
            name = builtin
        else:
            raise ValueError(f"Invalid hook config '{row_source}': 'name' is required.")

    if builtin is not None and builtin not in BUILTIN_HOOK_REGISTRY:
        valid = ", ".join(sorted(BUILTIN_HOOK_REGISTRY.keys()))
        raise ValueError(
            f"Invalid value for '{row_source}.builtin': expected one of [{valid}], got {builtin!r}."
        )
    if builtin is not None:
        try:
            validate_builtin_options(builtin, options)
        except KeyError:
            valid = ", ".join(sorted(BUILTIN_HOOK_REGISTRY.keys()))
            raise ValueError(
                f"Invalid value for '{row_source}.builtin': expected one of [{valid}], "
                f"got {builtin!r}."
            ) from None
        except ValueError as exc:
            raise ValueError(f"Invalid hook config '{row_source}': {exc}") from exc

    default_events = get_default_events(builtin) if builtin is not None else ()
    raw_event = cast("str | None", row.get("event"))

    # Determine which events to register for
    if raw_event is not None:
        # Explicit event specified - use only that one
        events_to_register: tuple[HookEventName, ...] = (
            _parse_event(raw_event, source=f"{row_source}.event"),
        )
    elif default_events:
        # No explicit event, use all default events for this builtin
        events_to_register = cast("tuple[HookEventName, ...]", default_events)
    else:
        raise ValueError(f"Invalid hook config '{row_source}': 'event' is required.")

    default_interval = BUILTIN_HOOK_REGISTRY[builtin].interval if builtin is not None else None
    interval = _parse_interval(
        cast("str | None", row.get("interval")) or default_interval,
        source=f"{row_source}.interval",
    )

    timeout_secs = cast("int | None", row.get("timeout_secs"))
    if timeout_secs is not None and timeout_secs <= 0:
        raise ValueError(
            f"Invalid value for '{row_source}.timeout_secs': expected int > 0, "
            f"got {timeout_secs!r}."
        )

    failure_policy = cast("str | None", row.get("failure_policy"))
    if failure_policy is not None and failure_policy not in _KNOWN_FAILURE_POLICIES:
        valid = ", ".join(sorted(_KNOWN_FAILURE_POLICIES))
        raise ValueError(
            f"Invalid value for '{row_source}.failure_policy': expected one of [{valid}], "
            f"got {failure_policy!r}."
        )
    resolved_failure_policy: FailurePolicy | None = None
    if failure_policy is not None:
        resolved_failure_policy = cast("FailurePolicy", failure_policy)

    exclude = cast("tuple[str, ...] | None", row.get("exclude")) or ()
    when = _parse_when(row.get("when"), source=f"{row_source}.when")

    # Create one Hook per event
    return tuple(
        Hook(
            name=name,
            event=event,
            source=source,
            command=command,
            builtin=builtin,
            timeout_secs=timeout_secs,
            interval=interval,
            enabled=cast("bool", row.get("enabled", True)),
            priority=cast("int", row.get("priority", 0)),
            failure_policy=resolved_failure_policy,
            require_serial=cast("bool", row.get("require_serial", False)),
            when=when,
            exclude=exclude,
            options=options,
            auto_registered=auto_registered,
            remote=remote,
        )
        for event in events_to_register
    )


def _hooks_from_payload(payload: dict[str, object], *, source: str) -> tuple[Hook, ...]:
    hooks: list[Hook] = []

    raw_hooks = payload.get("hooks")
    if raw_hooks is None:
        return tuple(hooks)

    rows = normalize_hooks_array(raw_hooks, source=f"{source}.hooks")
    for index, row in enumerate(rows, start=1):
        hooks.extend(
            _hook_from_row(
                row,
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
    """Load hook config with precedence: builtin < context < user < project < local."""

    paths = _resolve_hook_config_paths(
        project_root,
        user_config=user_config,
        authority=authority,
    )

    hooks: list[Hook] = []
    hooks.extend(_hooks_from_payload({}, source="builtin"))

    if paths.user_config is not None:
        hooks.extend(_hooks_from_payload(_read_toml(paths.user_config), source="user"))

    if paths.project_config is not None:
        hooks.extend(_hooks_from_payload(_read_toml(paths.project_config), source="project"))

    if paths.local_config.is_file():
        hooks.extend(_hooks_from_payload(_read_toml(paths.local_config), source="local"))

    return HooksConfig(hooks=_apply_name_overrides(tuple(hooks)))
