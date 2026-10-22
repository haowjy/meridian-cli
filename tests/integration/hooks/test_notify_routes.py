import json
import logging
from pathlib import Path

import pytest

from meridian.lib.config.models import MeridianConfig
from meridian.lib.idle.service import IdleService
from meridian.lib.notify.channels import REGISTRY
from meridian.lib.notify.emit import emit
from meridian.lib.notify.notice import SessionLabel, manual_notification
from meridian.lib.state.idle_store import IdleStore
from tests.support.notifications import RecordingChannel


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
        "notify.manual",
        manual_notification("Pick a store", "Tradeoffs in decisions.md", label),
        project_root=tmp_path,
        env={"MERIDIAN_SPAWN_ID": "p7"},
    )

    payload = json.loads((tmp_path / "got.json").read_text(encoding="utf-8"))
    assert payload["event_name"] == "notify.manual"
    assert payload["spawn"]["id"] == "p7"
    assert payload["notification"] == {
        "title": 'Pick a store — coder in project: "demo"',
        "body": 'Tradeoffs in decisions.md\ncoder in project: "demo"',
        "priority": 3,
        "email": True,
        "label": label.to_dict(),
        "attachments": [],
    }
    assert (tmp_path / "title.txt").read_text(encoding="utf-8") == (
        'Pick a store — coder in project: "demo"\n'
    )
    # The row replaced the default route and sent only to push (the none backend),
    # never to the smtp email backend.
    assert [(result.route, result.status) for result in report.results] == [
        ("desk", "sent"),
        ("notify", "none"),
    ]
    assert report.ok


@pytest.mark.parametrize(("exit_code", "recorded"), [(0, "sent"), (3, "failed")])
def test_idle_records_the_push_as_its_routes_delivered_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, recorded: str
) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    monkeypatch.setenv("MERIDIAN_NOTIFY_PUSH_BACKEND", "none")
    monkeypatch.delenv("MERIDIAN_HOOKS_ENABLED", raising=False)
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "desk"\n'
        'event = "notify.your_turn"\n'
        f'command = "exit {exit_code}"\n',
        encoding="utf-8",
    )
    now = [0]
    store = IdleStore(now_ms=lambda: now[0])
    idle = IdleService(
        store=store,
        config=MeridianConfig(),
        env={"MERIDIAN_SESSION_ROLE": "primary"},
        now_ms=lambda: now[0],
        project_root=tmp_path,
    )
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    now[0] = armed.push_at or 0

    idle.fire("push", harness="example", session="s1", stretch=1, anchor=1)

    # The default route delivered nothing (push is `none`), so `desk` decides.
    state = store.read("example", "s1")
    assert state is not None and state.done["push"] == recorded


def test_idle_without_injected_config_delivers_despite_an_invalid_hook_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    capture = RecordingChannel()
    monkeypatch.setitem(REGISTRY, "capture", capture)
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    monkeypatch.setenv("MERIDIAN_NOTIFY_PUSH_BACKEND", "capture")
    monkeypatch.setenv("MERIDIAN_NOTIFY_EMAIL_BACKEND", "none")
    monkeypatch.delenv("MERIDIAN_HOOKS_ENABLED", raising=False)
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "broken"\n'
        'command = "true"\n'
        'event = "spawn.finished"\n'
        'timeout_secs = "abc"\n',
        encoding="utf-8",
    )
    now = [0]
    store = IdleStore(now_ms=lambda: now[0])
    idle = IdleService(
        store=store,
        env={"MERIDIAN_SESSION_ROLE": "primary"},
        now_ms=lambda: now[0],
        project_root=tmp_path,
    )
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    now[0] = armed.push_at or 0

    with caplog.at_level(logging.WARNING, logger="meridian.lib.idle.service"):
        idle.fire("push", harness="example", session="s1", stretch=1, anchor=1)

    assert len(capture.deliveries) == 1
    event, notification = capture.deliveries[0]
    assert event == "notify.your_turn"
    assert notification.title.startswith("[idle] ")
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "meridian.lib.idle.service"
    ]
    assert len(warnings) == 1
    assert "dropped invalid hook rows" in warnings[0]
    state = store.read("example", "s1")
    assert state is not None and state.done["push"] == "sent"


def test_invalid_hook_row_keeps_a_valid_silencing_row_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = RecordingChannel()
    monkeypatch.setitem(REGISTRY, "capture", capture)
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    monkeypatch.setenv("MERIDIAN_NOTIFY_PUSH_BACKEND", "capture")
    monkeypatch.setenv("MERIDIAN_NOTIFY_EMAIL_BACKEND", "none")
    monkeypatch.delenv("MERIDIAN_HOOKS_ENABLED", raising=False)
    (tmp_path / "meridian.toml").write_text(
        "[[hooks]]\n"
        'builtin = "notify"\n'
        'event = "notify.manual"\n'
        "enabled = false\n"
        "[[hooks]]\n"
        'name = "broken"\n'
        'command = "true"\n'
        'event = "not.an.event"\n',
        encoding="utf-8",
    )

    report = emit(
        "notify.manual",
        manual_notification("Pick a store", None, SessionLabel()),
        project_root=tmp_path,
        env={},
    )

    assert capture.deliveries == []
    assert report.results == ()
    assert any("project.hooks[2]" in warning for warning in report.warnings)
    assert any("not.an.event" in warning for warning in report.warnings)
