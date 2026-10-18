"""ntfy HTTP notification channel."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from email.charset import BASE64, Charset
from email.header import Header
from pathlib import Path
from typing import cast
from urllib.parse import quote
from urllib.request import Request, urlopen

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels.base import SendResult
from meridian.plugin_api.types import Notification

type NtfyTransport = Callable[[Request], int]


def _urlopen_transport(request: Request) -> int:
    with urlopen(request, timeout=10) as response:
        return cast("int", response.getcode())


def _title_header(title: str) -> str:
    if title.isascii():
        return title
    charset = Charset("utf-8")
    charset.header_encoding = BASE64
    return Header(title, charset).encode(linesep=" ")


@dataclass(frozen=True)
class NtfyChannel:
    name = "ntfy"
    transport: NtfyTransport = _urlopen_transport

    def send(self, notification: Notification, cfg: NotifyConfig, *, event: str) -> SendResult:
        topic = cfg.ntfy_topic
        if topic is None:
            return SendResult(
                channel=self.name,
                status="none",
                warning="ntfy_topic is unset; ntfy delivery disabled",
            )

        url = f"{cfg.ntfy_server.rstrip('/')}/{quote(topic.strip(), safe='')}"
        headers = {
            "Title": _title_header(notification.title),
            "Priority": str(notification.priority),
            "Tags": event,
        }
        error = self._deliver(
            "message",
            Request(url, data=notification.body.encode("utf-8"), headers=headers, method="POST"),
        )
        if error is not None:
            return SendResult(channel=self.name, status="failed", error=error)

        # ntfy carries one file per message: each follows the text quietly, at low priority.
        file_headers = {**headers, "Priority": str(min(notification.priority, 2))}
        for path in map(Path, notification.attachments):
            try:
                data = path.read_bytes()
            except OSError as exc:
                return SendResult(
                    channel=self.name,
                    status="sent",
                    warning=f"{path.name}: {exc}",
                )
            error = self._deliver(
                path.name,
                Request(
                    url,
                    data=data,
                    headers={**file_headers, "Filename": _title_header(path.name)},
                    method="PUT",
                ),
            )
            if error is not None:
                return SendResult(channel=self.name, status="sent", warning=error)
        return SendResult(channel=self.name, status="sent")

    def _deliver(self, what: str, request: Request) -> str | None:
        try:
            status = self.transport(request)
        except Exception as exc:  # external transport boundary
            return f"{what}: {exc}"
        if not 200 <= status < 300:
            return f"{what}: ntfy returned HTTP {status}"
        return None

CHANNEL = NtfyChannel()

__all__ = ["CHANNEL", "NtfyChannel", "NtfyTransport"]
