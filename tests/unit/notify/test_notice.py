from pathlib import Path

from meridian.lib.notify import SessionLabel, manual_notice
from meridian.lib.notify.label import build_session_label


def test_manual_notice_uses_label_headline_with_tmux_and_work() -> None:
    notice = manual_notice(
        "build passed",
        SessionLabel(
            agent="coder",
            project="meridian-cli",
            work_id="idle-cache-notify",
            tmux_session="main",
        ),
    )

    assert notice.title == 'main: coder on work: "idle-cache-notify"'
    assert notice.body == "build passed"


def test_manual_title_moves_terminal_and_project_context_into_body() -> None:
    notice = manual_notice(
        "build passed",
        SessionLabel(harness="codex", project="meridian-cli", tmux_session="main"),
        title="Done",
    )

    assert notice.title == "Done"
    assert notice.body == 'build passed\nmain: codex in project: "meridian-cli"'
    assert "·" not in notice.title + notice.body


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
