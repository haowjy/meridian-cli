import json
from pathlib import Path

import pytest

from meridian.lib.notify.emit import emit
from meridian.lib.notify.notice import SessionLabel, manual_notice


def test_a_notification_reaches_command_hooks_and_its_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    monkeypatch.setenv("MERIDIAN_NOTIFY_PUSH_BACKEND", "none")
    monkeypatch.setenv("MERIDIAN_NOTIFY_EMAIL_BACKEND", "smtp")
    monkeypatch.delenv("MERIDIAN_HOOKS_ENABLED", raising=False)
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "desk"\n'
        'event = "notify.manual"\n'
        'command = "cat > got.json; printenv MERIDIAN_NOTIFICATION_TITLE > title.txt"\n'
        "[[hooks]]\n"
        'builtin = "notify"\n'
        'event = "notify.manual"\n'
        'options = { channels = ["push"], priority = 5 }\n',
        encoding="utf-8",
    )
    label = SessionLabel(agent="coder", project="demo")

    report = emit(
        manual_notice("Pick a store", "Tradeoffs in decisions.md", label),
        project_root=tmp_path,
        env={"MERIDIAN_SPAWN_ID": "p7"},
    )

    payload = json.loads((tmp_path / "got.json").read_text(encoding="utf-8"))
    assert payload["event_name"] == "notify.manual"
    assert payload["spawn"]["id"] == "p7"
    assert payload["notification"] == {
        "title": "Pick a store",
        "body": 'Tradeoffs in decisions.md\ncoder in project: "demo"',
        "priority": 3,
        "email": True,
        "label": label.to_dict(),
        "attachments": [],
    }
    assert (tmp_path / "title.txt").read_text(encoding="utf-8") == "Pick a store\n"
    # The route sent only to push (the none backend), never to the smtp email backend.
    assert [(result.channel, result.status) for result in report.results] == [
        ("hook:desk", "sent"),
        ("none", "none"),
    ]
    assert report.ok
