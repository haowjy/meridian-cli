from __future__ import annotations

import ssl
from email.message import EmailMessage
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels.gmail import GmailChannel
from meridian.lib.notify.channels.smtp import SMTPChannel
from meridian.lib.notify.notice import Notice
from meridian.lib.notify.service import send

if TYPE_CHECKING:
    import pytest


class _FakeSMTP:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.message: EmailMessage | None = None

    def __enter__(self) -> _FakeSMTP:
        self.events.append("connect")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc, traceback)
        self.events.append("close")

    def starttls(self, *, context: ssl.SSLContext) -> None:
        assert isinstance(context, ssl.SSLContext)
        self.events.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.events.append("login")
        self.login_args = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        self.events.append("send")
        self.message = message


class _FakeSMTPTransport:
    def __init__(self) -> None:
        self.connections: list[tuple[str, int, float]] = []
        self.client = _FakeSMTP()

    def __call__(self, host: str, port: int, timeout: float) -> _FakeSMTP:
        self.connections.append((host, port, timeout))
        return self.client


_NOTICE = Notice(
    title="[a2] meridian-cli · F1b",
    body="SMTP body",
    priority=3,
    email=True,
    kind="manual",
)


def _smtp_config(**overrides: object) -> NotifyConfig:
    values: dict[str, object] = {
        "smtp_host": "mail.example",
        "smtp_port": 2525,
        "smtp_user": "sender@example.com",
        "email_to": "recipient@example.com",
    }
    values.update(overrides)
    return NotifyConfig.model_validate(values)


def test_smtp_password_file_wins_over_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "smtp.pass"
    password_file.write_text("from-file\n", encoding="utf-8")
    password_file.chmod(0o600)
    monkeypatch.setenv("MERIDIAN_NOTIFY_SMTP_PASSWORD", "from-env")
    transport = _FakeSMTPTransport()

    result = SMTPChannel(transport=transport).send(
        _NOTICE,
        _smtp_config(smtp_password_file=password_file.as_posix()),
    )

    assert result.ok is True
    assert transport.connections == [("mail.example", 2525, 10.0)]
    assert transport.client.events == ["connect", "starttls", "login", "send", "close"]
    assert transport.client.login_args == ("sender@example.com", "from-file")
    assert transport.client.message is not None
    assert transport.client.message["From"] == "sender@example.com"
    assert transport.client.message["To"] == "recipient@example.com"
    assert transport.client.message["Subject"] == _NOTICE.title
    assert transport.client.message.get_content() == "SMTP body\n"


def test_smtp_uses_environment_when_password_file_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_NOTIFY_SMTP_PASSWORD", "from-env")
    transport = _FakeSMTPTransport()

    result = SMTPChannel(transport=transport).send(_NOTICE, _smtp_config())

    assert result.ok is True
    assert transport.client.login_args == ("sender@example.com", "from-env")


def test_smtp_refuses_group_or_world_readable_password_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "smtp.pass"
    password_file.write_text("from-file\n", encoding="utf-8")
    password_file.chmod(0o644)
    monkeypatch.setenv("MERIDIAN_NOTIFY_SMTP_PASSWORD", "from-env")
    transport = _FakeSMTPTransport()

    result = SMTPChannel(transport=transport).send(
        _NOTICE,
        _smtp_config(smtp_password_file=password_file.as_posix()),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "group/world readable" in result.error
    assert transport.connections == []


def test_missing_smtp_password_fails_the_only_email_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_NOTIFY_SMTP_PASSWORD", raising=False)
    cfg = _smtp_config(push_backend="none", email_backend="smtp")

    channel_result = SMTPChannel(transport=_FakeSMTPTransport()).send(_NOTICE, cfg)
    report = send(_NOTICE, cfg)

    assert channel_result.status == "failed"
    assert channel_result.error == "no SMTP password configured"
    assert [result.status for result in report.results] == ["none", "failed"]
    assert report.exit_code == 1


def test_gmail_fixes_host_and_port_but_shares_smtp_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_NOTIFY_SMTP_PASSWORD", "app-password")
    transport = _FakeSMTPTransport()

    result = GmailChannel(transport=transport).send(
        _NOTICE,
        _smtp_config(smtp_host="ignored.example", smtp_port=2465),
    )

    assert result.ok is True
    assert result.channel == "gmail"
    assert transport.connections == [("smtp.gmail.com", 587, 10.0)]
