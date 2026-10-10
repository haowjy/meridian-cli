from dataclasses import dataclass

import pytest

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels import REGISTRY
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.notice import Notice
from meridian.lib.notify.service import send


@dataclass(frozen=True)
class _ResultChannel:
    name: str
    result: SendResult

    def send(self, notice: Notice, cfg: NotifyConfig) -> SendResult:
        _ = (notice, cfg)
        return self.result


_NOTICE = Notice(title="Meridian", body="done", priority=3, email=True, kind="manual")


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

    report = send(_NOTICE, NotifyConfig(push_backend="ok", email_backend="broken"))

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

    report = send(_NOTICE, NotifyConfig(push_backend="broken-a", email_backend="broken-b"))

    assert report.ok is False
    assert report.exit_code == 1


def test_send_treats_all_none_as_success_with_warning() -> None:
    report = send(_NOTICE, NotifyConfig(push_backend="none", email_backend="none"))

    assert report.ok is True
    assert report.exit_code == 0
    assert [result.status for result in report.results] == ["none"]
    assert report.warnings == ("notification disabled: every selected backend is none",)
