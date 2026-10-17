from pathlib import Path

from meridian.lib.notify import SessionLabel, manual_notice
from meridian.lib.notify.label import build_session_label


def test_manual_notice_keeps_the_title_and_ends_the_body_with_its_source() -> None:
    label = SessionLabel(harness="codex", project="meridian-cli", tmux_session="main")

    detailed = manual_notice("Done", "build passed", label)
    bare = manual_notice("Done", None, label)

    assert (detailed.title, detailed.event) == ("Done", "notify.manual")
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
