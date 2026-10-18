"""Notification channel fakes shared across integration seams."""

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels.base import SendResult
from meridian.plugin_api.types import Notification


class RecordingChannel:
    """Record delivered notifications as event/notification pairs."""

    name = "capture"
    email = False

    def __init__(self) -> None:
        self.deliveries: list[tuple[str, Notification]] = []

    def send(
        self,
        notification: Notification,
        cfg: NotifyConfig,
        *,
        event: str,
    ) -> SendResult:
        _ = cfg
        self.deliveries.append((event, notification))
        return SendResult(channel=self.name, status="sent")


__all__ = ["RecordingChannel"]
