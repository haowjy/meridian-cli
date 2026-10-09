"""Notification channel contract and result value."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from meridian.lib.config.settings import NotifyConfig
    from meridian.lib.notify.notice import Notice

type SendStatus = Literal["sent", "failed", "none"]


@dataclass(frozen=True)
class SendResult:
    """Outcome from one selected channel."""

    channel: str
    status: SendStatus
    error: str | None = None
    warning: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "sent"

    def to_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "status": self.status,
            "ok": self.ok,
            "error": self.error,
            "warning": self.warning,
        }


class Channel(Protocol):
    """One notification transport registered by backend name."""

    @property
    def name(self) -> str: ...

    def send(self, notice: Notice, cfg: NotifyConfig) -> SendResult: ...


__all__ = ["Channel", "SendResult", "SendStatus"]
