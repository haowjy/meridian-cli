"""Single-source registry for builtin hook metadata."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from meridian.lib.hooks.types import NOTIFICATION_EVENTS, HookEventName

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class BuiltinHookMeta:
    """Metadata for a builtin hook."""

    name: str
    default_events: tuple[HookEventName, ...]
    interval: str | None
    required_options: tuple[str, ...]
    validate: Callable[[Mapping[str, object]], None] | None = None


def _validate_git_autosync_options(options: Mapping[str, object]) -> None:
    """Validate git-autosync options.

    Either 'remote' (remote-clone mode) or 'path' (local-only mode) must be
    present. Both are optional individually, but at least one is required.
    """

    remote = options.get("remote")
    path = options.get("path")

    if remote is not None:
        if not isinstance(remote, str) or not remote.strip():
            raise ValueError("'remote' must be a non-empty string")
    elif path is not None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("'path' must be a non-empty string")
    else:
        raise ValueError("git-autosync requires either 'remote' or 'path'")


def _validate_notify_options(options: Mapping[str, object]) -> None:
    """Validate a notification route: which channels, and an optional priority."""

    channels = options.get("channels")
    if channels is not None and (
        not isinstance(channels, list | tuple)
        or not all(
            isinstance(channel, str) and channel.strip()
            for channel in cast("Sequence[object]", channels)
        )
    ):
        raise ValueError("'channels' must be a list of channel names")
    priority = options.get("priority")
    if priority is not None and (
        isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 5
    ):
        raise ValueError("'priority' must be an integer from 1 to 5")


BUILTIN_HOOK_REGISTRY: dict[str, BuiltinHookMeta] = {
    "git-autosync": BuiltinHookMeta(
        name="git-autosync",
        default_events=("spawn.start", "spawn.finalized", "work.started", "work.done"),
        interval=None,
        required_options=(),
        validate=_validate_git_autosync_options,
    ),
    "notify": BuiltinHookMeta(
        name="notify",
        default_events=NOTIFICATION_EVENTS,
        interval=None,
        required_options=(),
        validate=_validate_notify_options,
    ),
}


def get_builtin_meta(name: str) -> BuiltinHookMeta | None:
    """Get metadata for a builtin hook."""

    return BUILTIN_HOOK_REGISTRY.get(name)


def get_default_events(name: str) -> tuple[HookEventName, ...]:
    """Get default events for a builtin hook."""

    meta = get_builtin_meta(name)
    if meta is None:
        return ()
    return meta.default_events


def validate_builtin_options(name: str, options: Mapping[str, object]) -> None:
    """Validate options for a builtin hook.

    Raises:
        ValueError: If validation fails.
        KeyError: If builtin is not registered.
    """

    meta = BUILTIN_HOOK_REGISTRY.get(name)
    if meta is None:
        raise KeyError(f"Unknown builtin hook: {name}")

    for required in meta.required_options:
        if required not in options:
            raise ValueError(f"'{required}' is required for {name}")

    if meta.validate is not None:
        meta.validate(options)
