"""Single registration point for notification channels."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, cast

from meridian.lib.notify.channels.base import Channel, SendResult

if TYPE_CHECKING:
    from meridian.lib.config.models import NotifyConfig
    from meridian.plugin_api.types import Notification


@dataclass(frozen=True)
class RegisteredChannel:
    """A backend by name, imported when it first sends."""

    name: str
    module: str
    # Email transports carry only notifications that want email.
    email: bool = False

    def send(self, notification: Notification, cfg: NotifyConfig, *, event: str) -> SendResult:
        channel = cast("Channel", import_module(self.module).CHANNEL)
        return channel.send(notification, cfg, event=event)


REGISTRY: dict[str, RegisteredChannel] = {
    "gmail": RegisteredChannel("gmail", "meridian.lib.notify.channels.gmail", email=True),
    "none": RegisteredChannel("none", "meridian.lib.notify.channels.none"),
    "ntfy": RegisteredChannel("ntfy", "meridian.lib.notify.channels.ntfy"),
    "smtp": RegisteredChannel("smtp", "meridian.lib.notify.channels.smtp", email=True),
}


def normalize_channel_name(name: str, *, aliases: tuple[str, ...] = ()) -> str:
    """Normalize a backend name and reject names no delivery path can resolve."""

    normalized = name.strip().lower()
    available = frozenset(REGISTRY) | frozenset(aliases)
    if normalized not in available:
        choices = ", ".join(sorted(available))
        raise ValueError(
            f"Unknown notification backend {name!r}. Available backends: {choices}."
        )
    return normalized


def resolve_channel(name: str) -> RegisteredChannel:
    """Resolve a configured backend or raise a configuration-oriented error."""
    return REGISTRY[normalize_channel_name(name)]


__all__ = ["REGISTRY", "RegisteredChannel", "normalize_channel_name", "resolve_channel"]
