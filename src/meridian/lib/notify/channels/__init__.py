"""Single registration point for notification channels."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import cast

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels.base import Channel, SendResult
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


def resolve_channel(name: str) -> RegisteredChannel:
    """Resolve a configured backend or raise a configuration-oriented error."""
    normalized = name.strip().lower()
    channel = REGISTRY.get(normalized)
    if channel is None:
        available = ", ".join(sorted(REGISTRY))
        raise ValueError(
            f"Unknown notification backend {name!r}. Available backends: {available}."
        )
    return channel


__all__ = ["REGISTRY", "RegisteredChannel", "resolve_channel"]
