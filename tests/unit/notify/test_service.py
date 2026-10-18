from dataclasses import dataclass

import pytest

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels import REGISTRY
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.service import send
from meridian.plugin_api.types import Notification


@dataclass(frozen=True)
class _ResultChannel:
    name: str
    result: SendResult
    email: bool = False

    def send(self, notification: Notification, cfg: NotifyConfig, *, event: str) -> SendResult:
        _ = (notification, cfg, event)
        return self.result


_NOTICE = Notification(title="Meridian", body="done")


def test_send_succeeds_when_one_channel_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        REGISTRY,
        "ok",
        _ResultChannel("ok", SendResult(channel="ok", status="sent")),
    )
    monkeypatch.setitem(
        REGISTRY,
        "broken",
        _ResultChannel("broken", SendResult(channel="broken", status="failed", error="down")),
    )

    report = send(
        _NOTICE, NotifyConfig(push_backend="ok", email_backend="broken"), event="notify.manual"
    )

    assert report.ok is True
    assert report.exit_code == 0
    assert [result.status for result in report.results] == ["sent", "failed"]


def test_send_fails_when_all_channels_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        REGISTRY,
        "broken-a",
        _ResultChannel(
            "broken-a", SendResult(channel="broken-a", status="failed", error="down")
        ),
    )
    monkeypatch.setitem(
        REGISTRY,
        "broken-b",
        _ResultChannel(
            "broken-b", SendResult(channel="broken-b", status="failed", error="down")
        ),
    )

    report = send(
        _NOTICE,
        NotifyConfig(push_backend="broken-a", email_backend="broken-b"),
        event="notify.manual",
    )

    assert report.ok is False
    assert report.exit_code == 1


def test_send_treats_all_none_as_quiet_success() -> None:
    report = send(
        _NOTICE, NotifyConfig(push_backend="none", email_backend="none"), event="notify.manual"
    )

    assert report.ok is True
    assert report.exit_code == 0
    assert [result.status for result in report.results] == ["none"]
    assert report.all_none
    assert report.warnings == ()


def test_an_email_backend_named_directly_still_honours_no_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = SendResult(channel="mail", status="sent")
    monkeypatch.setitem(REGISTRY, "mail", _ResultChannel("mail", sent, email=True))
    quiet = Notification(title="Meridian", body="done", email=False)

    report = send(quiet, NotifyConfig(), event="notify.manual", channels=["mail"])

    assert report.results == ()
