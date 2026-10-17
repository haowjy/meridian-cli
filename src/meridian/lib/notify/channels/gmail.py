"""Gmail preset for the SMTP notification channel."""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.channels.smtp import SMTPChannel, SMTPTransport, open_smtp
from meridian.plugin_api.types import Notification


@dataclass(frozen=True)
class GmailChannel:
    """Deliver through Gmail's STARTTLS submission endpoint."""

    name = "gmail"
    transport: SMTPTransport = open_smtp

    def send(self, notification: Notification, cfg: NotifyConfig, *, event: str) -> SendResult:
        return SMTPChannel(
            name=self.name,
            transport=self.transport,
            host="smtp.gmail.com",
            port=587,
        ).send(notification, cfg, event=event)


CHANNEL = GmailChannel()

__all__ = ["CHANNEL", "GmailChannel"]
