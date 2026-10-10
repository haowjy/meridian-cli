from pathlib import Path

from meridian.lib.notify import SessionLabel, manual_notice
from meridian.lib.notify.label import build_session_label


def test_manual_notice_uses_label_headline_and_tmux_footer() -> None:
    notice = manual_notice(
        "build passed",
        SessionLabel(
            agent="coder",
            project="meridian-cli",
            work_id="idle-cache-notify",
            tmux_session="main",
        ),
    )

    assert notice.title == "coder on idle-cache-notify"
    assert notice.body == "build passed\ntmux: main"


def test_manual_title_moves_description_before_final_tmux_line() -> None:
    notice = manual_notice(
        "build passed",
        SessionLabel(harness="codex", project="meridian-cli", tmux_session="main"),
        title="Done",
    )

    assert notice.title == "Done"
    assert notice.body == "build passed\ncodex in meridian-cli\ntmux: main"
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

    assert label.headline == "tech-lead on idle-cache-notify"
    assert label.footer == "tmux: main"
