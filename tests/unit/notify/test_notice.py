from pathlib import Path

import pytest

from meridian.lib.notify import SessionLabel, manual_notification
from meridian.lib.notify.label import build_session_label


def test_manual_notification_puts_the_message_first_and_source_in_the_title() -> None:
    label = SessionLabel(harness="codex", project="meridian-cli", tmux_session="main")

    detailed = manual_notification("Done", "build passed", label)
    bare = manual_notification("Done", None, label)

    assert (detailed.title, detailed.label) == (
        'Done — main: codex in project: "meridian-cli"',
        label.to_dict(),
    )
    assert detailed.body == 'build passed\nmain: codex in project: "meridian-cli"'
    assert bare.body == 'main: codex in project: "meridian-cli"'
    assert "·" not in detailed.title + detailed.body


def test_session_label_resolves_current_work_and_falls_back_to_launch_env() -> None:
    label = build_session_label(
        environ={
            "MERIDIAN_SESSION_AGENT": "tech-lead",
            "_MERIDIAN_HARNESS": "codex",
            "MERIDIAN_ACTIVE_WORK_ID": "launch-work",
            "MERIDIAN_CHAT_ID": "c1",
            "MERIDIAN_PROJECT_DIR": "/repo/meridian-cli",
            "TMUX_PANE": "%3",
        },
        cwd=Path("/fallback"),
        resolve_tmux_session=lambda pane: "main" if pane == "%3" else None,
        resolve_runtime_root=lambda project_root: (
            Path("/state") if project_root == Path("/repo/meridian-cli") else None
        ),
        resolve_session_work=lambda runtime_root, chat_id: (
            runtime_root == Path("/state") and chat_id == "c1",
            "current-work",
        ),
    )

    assert label.headline == 'main: tech-lead (codex) on work: "current-work"'
    assert label.to_dict()["tmux_session"] == "main"

    fallback = build_session_label(
        environ={
            "MERIDIAN_ACTIVE_WORK_ID": "launch-work",
            "MERIDIAN_CHAT_ID": "missing",
        },
        cwd=Path("/fallback"),
        resolve_runtime_root=lambda _project_root: Path("/state"),
        resolve_session_work=lambda _runtime_root, _chat_id: (False, None),
    )
    assert fallback.work_id == "launch-work"

    cleared = build_session_label(
        environ={
            "MERIDIAN_ACTIVE_WORK_ID": "launch-work",
            "MERIDIAN_CHAT_ID": "c1",
        },
        cwd=Path("/fallback"),
        resolve_runtime_root=lambda _project_root: Path("/state"),
        resolve_session_work=lambda _runtime_root, _chat_id: (True, None),
    )
    assert cleared.work_id is None


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
