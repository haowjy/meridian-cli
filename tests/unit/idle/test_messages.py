from __future__ import annotations

import pytest

from meridian.lib.idle.excerpts import trim_turn_excerpts
from meridian.lib.idle.messages import compaction_notice, stage_notice
from meridian.lib.notify import SessionLabel
from meridian.lib.state.idle_store import IdleSchedule, IdleState


def _state(**updates: object) -> IdleState:
    values: dict[str, object] = {
        "harness": "codex",
        "session": "s1",
        "stretch": 1,
        "stretch_open": True,
        "anchor": 1,
        "idle_since_ms": 1,
        "schedule": IdleSchedule(),
        "updated_at_ms": 1,
        "last_user_text": "can u first test/install it locally?",
        "last_assistant_text": "Your installed meridian is exactly the PR's latest code.",
    }
    values.update(updates)
    return IdleState.model_validate(values)


def test_push_warn_and_missing_message_rendering() -> None:
    label = SessionLabel(
        agent="tech-lead",
        harness="codex",
        project="meridian-cli",
        work_id="idle-cache-notify",
        tmux_session="main",
    )
    push = stage_notice(
        "push",
        _state(),
        label,
        include_messages=True,
        warn_minutes=5,
        warn_email=True,
    )
    warn = stage_notice(
        "warn",
        _state(),
        label,
        include_messages=True,
        warn_minutes=5,
        warn_email=True,
    )
    hidden = stage_notice(
        "push",
        _state(),
        SessionLabel(agent="coder", project="meridian-cli"),
        include_messages=False,
        warn_minutes=5,
        warn_email=False,
    )

    assert push.title == "tech-lead on idle-cache-notify"
    assert push.body == (
        "You: can u first test/install it locally?\n"
        "Assistant: Your installed meridian is exactly the PR's latest code.\n"
        "tmux: main"
    )
    assert warn.body.startswith("Cache goes cold in 5 min\nYou: ")
    assert warn.body.endswith("\ntmux: main")
    assert hidden == push.__class__(
        title="coder in meridian-cli",
        body="Your turn",
        priority=3,
        email=False,
        kind="idle",
    )


@pytest.mark.parametrize(
    ("result", "detail", "event"),
    [
        ("ok", "ignored detail", "Compacted"),
        ("failed", "timeout", "Compaction failed: timeout"),
        ("vetoed", "draft", "Compaction skipped: draft"),
    ],
)
def test_compaction_rendering(
    result: str,
    detail: str | None,
    event: str,
) -> None:
    notice = compaction_notice(
        result,  # type: ignore[arg-type]
        detail,
        _state(),
        SessionLabel(harness="claude", project="meridian-cli", tmux_session="dev"),
    )

    assert notice.title == "claude in meridian-cli"
    assert notice.body == f"{event}\ntmux: dev"
    assert "You:" not in notice.body
    assert "Assistant:" not in notice.body


def test_label_fallbacks_and_rendered_notices_have_no_separator_glyph() -> None:
    labels = (
        SessionLabel(agent="coder", project="meridian-cli"),
        SessionLabel(harness="pi", work_id="idle-cache-notify"),
        SessionLabel(),
    )
    assert [label.headline for label in labels] == [
        "coder in meridian-cli",
        "pi on idle-cache-notify",
        "Meridian",
    ]

    notices = [
        stage_notice(
            stage,
            _state(last_user_text=None, last_assistant_text=None),
            label,
            include_messages=True,
            warn_minutes=5,
            warn_email=False,
        )
        for stage in ("push", "warn")
        for label in labels
    ]
    assert notices[0].body == "Your turn"
    assert all("·" not in notice.title and "·" not in notice.body for notice in notices)


def test_excerpt_trimming_collapses_whitespace_cuts_at_words_and_strips_reminders() -> None:
    user, assistant = trim_turn_excerpts(
        "  human prompt\n\n<system-reminder>injected hook text</system-reminder> "
        + "word " * 40,
        "reply\nwith\tspacing " + "answer " * 60,
    )

    assert user is not None and len(user) <= 120 and user.endswith("…")
    assert assistant is not None and len(assistant) <= 280 and assistant.endswith("…")
    assert "system-reminder" not in user
    assert "  " not in user + assistant
