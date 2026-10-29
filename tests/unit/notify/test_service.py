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


@pytest.mark.parametrize(
    ("channels", "push", "email", "ok", "exit_code", "statuses"),
    [
        (
            {"ok": "sent", "broken": "failed"},
            "ok",
            "broken",
            True,
            0,
            ["sent", "failed"],
        ),
        (
            {"broken-a": "failed", "broken-b": "failed"},
            "broken-a",
            "broken-b",
            False,
            1,
            ["failed", "failed"],
        ),
    ],
    ids=["one-succeeds", "all-fail"],
)
def test_send_aggregates_channel_results(
    monkeypatch: pytest.MonkeyPatch,
    channels: dict[str, str],
    push: str,
    email: str,
    ok: bool,
    exit_code: int,
    statuses: list[str],
) -> None:
    for name, status in channels.items():
        monkeypatch.setitem(
            REGISTRY,
            name,
            _ResultChannel(
                name,
                SendResult(
                    channel=name,
                    status=status,  # type: ignore[arg-type]
                    error=None if status == "sent" else "down",
                ),
            ),
        )

    report = send(
        _NOTICE, NotifyConfig(push_backend=push, email_backend=email), event="notify.manual"
    )

    assert report.ok is ok
    assert report.exit_code == exit_code
    assert [result.status for result in report.results] == statuses


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
