from urllib.request import Request

import pytest

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels import resolve_channel
from meridian.lib.notify.channels.ntfy import NtfyChannel
from meridian.lib.notify.notice import Notice


def test_registry_resolves_builtin_channels() -> None:
    assert resolve_channel("ntfy").name == "ntfy"
    assert resolve_channel("none").name == "none"
    assert resolve_channel("smtp").name == "smtp"
    assert resolve_channel("gmail").name == "gmail"


def test_registry_rejects_unknown_channel() -> None:
    with pytest.raises(
        ValueError,
        match=r"Unknown notification backend 'carrier-pigeon'.*none.*ntfy",
    ):
        resolve_channel("carrier-pigeon")


def test_ntfy_builds_expected_post_request() -> None:
    captured: list[Request] = []

    def transport(request: Request) -> int:
        captured.append(request)
        return 204

    channel = NtfyChannel(transport=transport)
    result = channel.send(
        Notice(
            title="[a2] meridian-cli · idle-cache-notify",
            body="f1a smoke",
            priority=4,
            email=True,
            kind="manual",
        ),
        NotifyConfig(ntfy_server="https://push.example/root/", ntfy_topic="topic with space"),
    )

    assert result.ok is True
    assert result.channel == "ntfy"
    assert len(captured) == 1
    request = captured[0]
    assert request.full_url == "https://push.example/root/topic%20with%20space"
    assert request.method == "POST"
    assert request.data == b"f1a smoke"
    assert request.get_header("Title") == "[a2] meridian-cli · idle-cache-notify"
    assert request.get_header("Priority") == "4"
    assert request.get_header("Tags") == "manual"


def test_ntfy_without_topic_degrades_to_none() -> None:
    channel = NtfyChannel(transport=lambda _request: pytest.fail("transport called"))

    result = channel.send(
        Notice(title="Meridian", body="hello", priority=3, email=False, kind="manual"),
        NotifyConfig(ntfy_topic=None),
    )

    assert result.status == "none"
    assert result.warning == "ntfy_topic is unset; ntfy delivery disabled"
