"""Single registration point for notification channels."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import cast

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels.base import Channel, SendResult
from meridian.lib.notify.notice import Notice


@dataclass(frozen=True)
class _LazyChannel:
    name: str
    module: str

    def send(self, notice: Notice, cfg: NotifyConfig) -> SendResult:
        channel = cast("Channel", import_module(self.module).CHANNEL)
        return channel.send(notice, cfg)


REGISTRY: dict[str, Channel] = {
    "command": _LazyChannel("command", "meridian.lib.notify.channels.command"),
    "gmail": _LazyChannel("gmail", "meridian.lib.notify.channels.gmail"),
    "none": _LazyChannel("none", "meridian.lib.notify.channels.none"),
    "ntfy": _LazyChannel("ntfy", "meridian.lib.notify.channels.ntfy"),
    "smtp": _LazyChannel("smtp", "meridian.lib.notify.channels.smtp"),
}


def resolve_channel(name: str) -> Channel:
    """Resolve a configured backend or raise a configuration-oriented error."""
    normalized = name.strip().lower()
    channel = REGISTRY.get(normalized)
    if channel is None:
        available = ", ".join(sorted(REGISTRY))
        raise ValueError(
            f"Unknown notification backend {name!r}. Available backends: {available}."
        )
    return channel


__all__ = ["REGISTRY", "resolve_channel"]
