from __future__ import annotations

import ssl
from email.message import EmailMessage
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels.smtp import SMTPChannel
from meridian.plugin_api.types import Notification

if TYPE_CHECKING:
    import pytest


class _FakeSMTP:
    def __init__(self) -> None:
        self.login_args: tuple[str, str] | None = None
        self.messages: list[EmailMessage] = []

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc, traceback)

    def starttls(self, *, context: ssl.SSLContext) -> None:
        assert isinstance(context, ssl.SSLContext)

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        self.messages.append(message)


class _FakeSMTPTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.client = _FakeSMTP()

    def __call__(self, host: str, port: int, timeout: float) -> _FakeSMTP:
        _ = (host, port, timeout)
        self.calls += 1
        return self.client


_NOTICE = Notification(title="Meridian", body="SMTP body")


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
        event="notify.manual",
    )

    assert result.ok is True
    assert transport.client.login_args == ("sender@example.com", "from-file")


def test_smtp_uses_environment_when_password_file_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_NOTIFY_SMTP_PASSWORD", "from-env")
    transport = _FakeSMTPTransport()

    result = SMTPChannel(transport=transport).send(_NOTICE, _smtp_config(), event="notify.manual")

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
        event="notify.manual",
    )

    assert result.status == "failed"
    assert transport.calls == 0


def test_smtp_attaches_every_file_with_a_guessed_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_NOTIFY_SMTP_PASSWORD", "from-env")
    (tmp_path / "decisions.md").write_text("# Options\n", encoding="utf-8")
    (tmp_path / "trace.bin").write_bytes(b"\x00\x01")
    transport = _FakeSMTPTransport()
    notice = Notification(
        title="Pick auth storage",
        body="Tradeoffs attached",
        attachments=((tmp_path / "decisions.md").as_posix(), (tmp_path / "trace.bin").as_posix()),
    )

    result = SMTPChannel(transport=transport).send(notice, _smtp_config(), event="notify.manual")

    attached = [
        (part.get_filename(), part.get_content_type(), part.get_content())
        for part in transport.client.messages[0].iter_attachments()
    ]
    assert result.ok is True
    assert attached == [
        ("decisions.md", "text/markdown", "# Options\n"),
        ("trace.bin", "application/octet-stream", b"\x00\x01"),
    ]
