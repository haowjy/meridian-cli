"""SMTP email notification channel."""

from __future__ import annotations

import os
import smtplib
import ssl
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from types import TracebackType
from typing import Protocol, cast

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.notice import Notice
from meridian.lib.platform import get_home_path

_PASSWORD_ENV = "MERIDIAN_NOTIFY_SMTP_PASSWORD"
_SMTP_TIMEOUT_SECONDS = 10.0


class SMTPConnection(Protocol):
    """The smtplib operations needed by the channel."""

    def __enter__(self) -> SMTPConnection: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> object: ...

    def starttls(self, *, context: ssl.SSLContext) -> object: ...

    def login(self, user: str, password: str) -> object: ...

    def send_message(self, message: EmailMessage) -> object: ...


type SMTPTransport = Callable[[str, int, float], SMTPConnection]


def open_smtp(host: str, port: int, timeout: float) -> SMTPConnection:
    """Open the stdlib SMTP transport behind the injectable channel boundary."""
    return cast("SMTPConnection", smtplib.SMTP(host, port, timeout=timeout))


def _password_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.parts and path.parts[0] == "~":
        return get_home_path().joinpath(*path.parts[1:])
    return path


def _read_password_file(raw_path: str) -> str:
    path = _password_path(raw_path)
    with path.open(encoding="utf-8") as handle:
        mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            raise ValueError(
                f"SMTP password file {path} is group/world readable; use chmod 600"
            )
        return handle.read().rstrip("\r\n")


def _resolve_password(cfg: NotifyConfig, environ: Mapping[str, str]) -> str:
    if cfg.smtp_password_file is not None:
        password = _read_password_file(cfg.smtp_password_file)
    else:
        password = environ.get(_PASSWORD_ENV, "")
    if not password:
        raise ValueError("no SMTP password configured")
    return password


def _required(value: str | None, name: str) -> str:
    normalized = value.strip() if value is not None else ""
    if not normalized:
        raise ValueError(f"{name} is required for SMTP delivery")
    return normalized


@dataclass(frozen=True)
class SMTPChannel:
    """Deliver a notice through an authenticated STARTTLS SMTP server."""

    name: str = "smtp"
    transport: SMTPTransport = open_smtp
    host: str | None = None
    port: int | None = None

    def send(self, notice: Notice, cfg: NotifyConfig) -> SendResult:
        try:
            user = _required(cfg.smtp_user, "smtp_user")
            recipient = _required(cfg.email_to, "email_to")
            password = _resolve_password(cfg, os.environ)
            host = self.host if self.host is not None else cfg.smtp_host
            port = self.port if self.port is not None else cfg.smtp_port

            message = EmailMessage()
            message["From"] = _required(cfg.email_from, "email_from") if cfg.email_from else user
            message["To"] = recipient
            message["Subject"] = notice.title
            message.set_content(notice.body)

            with self.transport(host, port, _SMTP_TIMEOUT_SECONDS) as client:
                client.starttls(context=ssl.create_default_context())
                client.login(user, password)
                client.send_message(message)
        except Exception as exc:  # external transport and credential-file boundary
            return SendResult(channel=self.name, status="failed", error=str(exc))

        return SendResult(channel=self.name, status="sent")


CHANNEL = SMTPChannel()

__all__ = ["CHANNEL", "SMTPChannel", "SMTPConnection", "SMTPTransport", "open_smtp"]
