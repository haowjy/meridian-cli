from pathlib import Path

import pytest

from meridian.lib.notify import SessionLabel, manual_notification
from meridian.lib.notify.label import build_session_label


def test_manual_notification_keeps_the_title_and_ends_the_body_with_its_source() -> None:
    label = SessionLabel(harness="codex", project="meridian-cli", tmux_session="main")

    detailed = manual_notification("Done", "build passed", label)
    bare = manual_notification("Done", None, label)

    assert (detailed.title, detailed.label) == ("Done", label.to_dict())
    assert detailed.body == 'build passed\nmain: codex in project: "meridian-cli"'
    assert bare.body == 'main: codex in project: "meridian-cli"'
    assert "·" not in detailed.title + detailed.body


def test_session_label_resolves_agent_harness_work_and_tmux_from_launch_env() -> None:
    label = build_session_label(
        environ={
            "MERIDIAN_SESSION_AGENT": "tech-lead",
            "_MERIDIAN_HARNESS": "codex",
            "MERIDIAN_ACTIVE_WORK_ID": "idle-cache-notify",
            "MERIDIAN_PROJECT_DIR": "/repo/meridian-cli",
            "TMUX_PANE": "%3",
        },
        cwd=Path("/fallback"),
        resolve_tmux_session=lambda pane: "main" if pane == "%3" else None,
    )

    assert label.headline == 'main: tech-lead on work: "idle-cache-notify"'
    assert label.to_dict()["tmux_session"] == "main"


@pytest.mark.parametrize(
    ("title", "priority", "attachment", "error"),
    [
        (" ", 3, None, "title must not be empty"),
        ("Done", 6, None, "priority must be between 1 and 5"),
        ("Done", 3, "missing.txt", "attachment is not a readable file"),
    ],
)
def test_manual_notification_refuses_what_no_route_could_send(
    tmp_path: Path, title: str, priority: int, attachment: str | None, error: str
) -> None:
    attachments = (tmp_path / attachment,) if attachment else ()

    with pytest.raises(ValueError, match=error):
        manual_notification(
            title, None, SessionLabel(), priority=priority, attachments=attachments
        )
