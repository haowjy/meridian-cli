"""Explicitly disabled notification channel."""

from __future__ import annotations

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.notice import Notice


class NoneChannel:
    name = "none"

    def send(self, notice: Notice, cfg: NotifyConfig) -> SendResult:
        _ = (notice, cfg)
        return SendResult(channel=self.name, status="none")


CHANNEL = NoneChannel()

__all__ = ["CHANNEL", "NoneChannel"]
