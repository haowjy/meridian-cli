"""Gmail preset for the SMTP notification channel."""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.channels.smtp import SMTPChannel, SMTPTransport, open_smtp
from meridian.lib.notify.notice import Notice


@dataclass(frozen=True)
class GmailChannel:
    """Deliver through Gmail's STARTTLS submission endpoint."""

    name = "gmail"
    transport: SMTPTransport = open_smtp

    def send(self, notice: Notice, cfg: NotifyConfig) -> SendResult:
        return SMTPChannel(
            name=self.name,
            transport=self.transport,
            host="smtp.gmail.com",
            port=587,
        ).send(notice, cfg)


CHANNEL = GmailChannel()

__all__ = ["CHANNEL", "GmailChannel"]
