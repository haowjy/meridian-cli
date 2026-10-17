"""Explicitly disabled notification channel."""

from __future__ import annotations

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels.base import SendResult
from meridian.plugin_api.types import Notification


class NoneChannel:
    name = "none"

    def send(self, notification: Notification, cfg: NotifyConfig, *, event: str) -> SendResult:
        _ = (notification, cfg, event)
        return SendResult(channel=self.name, status="none")


CHANNEL = NoneChannel()

__all__ = ["CHANNEL", "NoneChannel"]
