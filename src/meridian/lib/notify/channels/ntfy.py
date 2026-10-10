"""ntfy HTTP notification channel."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from email.charset import BASE64, Charset
from email.header import Header
from typing import cast
from urllib.parse import quote
from urllib.request import Request, urlopen

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.notice import Notice

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

    def send(self, notice: Notice, cfg: NotifyConfig) -> SendResult:
        topic = cfg.ntfy_topic
        if topic is None:
            return SendResult(
                channel=self.name,
                status="none",
                warning="ntfy_topic is unset; ntfy delivery disabled",
            )

        try:
            url = f"{cfg.ntfy_server.rstrip('/')}/{quote(topic.strip(), safe='')}"
            request = Request(
                url,
                data=notice.body.encode("utf-8"),
                headers={
                    "Title": _title_header(notice.title),
                    "Priority": str(notice.priority),
                    "Tags": notice.kind,
                },
                method="POST",
            )
            status = self.transport(request)
        except Exception as exc:  # external transport boundary
            return SendResult(channel=self.name, status="failed", error=str(exc))

        if not 200 <= status < 300:
            return SendResult(
                channel=self.name,
                status="failed",
                error=f"ntfy returned HTTP {status}",
            )
        return SendResult(channel=self.name, status="sent")


CHANNEL = NtfyChannel()

__all__ = ["CHANNEL", "NtfyChannel", "NtfyTransport"]
