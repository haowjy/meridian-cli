from meridian.lib.notify import SessionLabel, manual_notification


def test_manual_notification_titles_the_sender_then_the_message() -> None:
    label = SessionLabel(harness="codex", project="meridian-cli", tmux_session="main")

    detailed = manual_notification("Done", "build passed", label)
    bare = manual_notification("Done", None, label)

    assert (detailed.title, detailed.label) == (
        "main: codex Done",
        label.to_dict(),
    )
    assert detailed.body == 'build passed\nmain: codex in project: "meridian-cli"'
    assert bare.body == 'main: codex in project: "meridian-cli"'
    assert "·" not in detailed.title + detailed.body
