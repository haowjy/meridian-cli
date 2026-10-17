from pathlib import Path
from urllib.request import Request

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels.ntfy import NtfyChannel
from meridian.plugin_api.types import Notification

_CONFIG = NotifyConfig.model_validate({"ntfy_server": "https://ntfy.example", "ntfy_topic": "t"})


def _notice(*attachments: Path) -> Notification:
    return Notification(
        title="Pick auth storage",
        body="Tradeoffs attached",
        priority=4,
        attachments=tuple(path.as_posix() for path in attachments),
    )


def test_ntfy_sends_the_text_then_one_message_per_file(tmp_path: Path) -> None:
    (tmp_path / "decisions.md").write_text("# Options\n", encoding="utf-8")
    (tmp_path / "diagram.png").write_bytes(b"\x89PNG")
    requests: list[Request] = []

    def transport(request: Request) -> int:
        requests.append(request)
        return 200

    result = NtfyChannel(transport=transport).send(
        _notice(tmp_path / "decisions.md", tmp_path / "diagram.png"), _CONFIG, event="notify.manual"
    )

    assert result.ok
    assert [
        (
            request.get_method(),
            request.data,
            request.get_header("Filename"),
            request.get_header("Priority"),
        )
        for request in requests
    ] == [
        ("POST", b"Tradeoffs attached", None, "4"),
        ("PUT", b"# Options\n", "decisions.md", "2"),
        ("PUT", b"\x89PNG", "diagram.png", "2"),
    ]
    assert {request.get_header("Title") for request in requests} == {"Pick auth storage"}


def test_ntfy_names_the_file_a_server_rejects(tmp_path: Path) -> None:
    (tmp_path / "big.pdf").write_bytes(b"%PDF")

    result = NtfyChannel(transport=lambda request: 413 if request.data == b"%PDF" else 200).send(
        _notice(tmp_path / "big.pdf"), _CONFIG, event="notify.manual"
    )

    assert (result.status, result.error) == ("failed", "big.pdf: ntfy returned HTTP 413")
