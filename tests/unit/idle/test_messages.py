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
        now_ms=1,
        warn_email=True,
    )
    warn = stage_notice(
        "warn",
        _state(),
        label,
        include_messages=True,
        now_ms=1,
        warn_email=True,
    )
    hidden = stage_notice(
        "push",
        _state(),
        SessionLabel(agent="coder", project="meridian-cli"),
        include_messages=False,
        now_ms=1,
        warn_email=False,
    )

    assert push.title == '[idle] main: tech-lead on work: "idle-cache-notify"'
    assert push.body == (
        "You: can u first test/install it locally?\n"
        "Assistant: Your installed meridian is exactly the PR's latest code."
    )
    assert warn.title == '[cache warning] main: tech-lead on work: "idle-cache-notify"'
    assert warn.body.startswith("Cache expiry warning: reply to keep it warm\nYou: ")
    assert hidden == push.__class__(
        title='[idle] coder in project: "meridian-cli"',
        body="Your turn",
        priority=3,
        email=False,
        kind="idle",
    )


@pytest.mark.parametrize(
    ("result", "detail", "reason", "event"),
    [
        ("ok", "ignored detail", "compacted", "Compacted"),
        ("failed", "timeout", "compaction failed", "Compaction failed: timeout"),
    ],
)
def test_compaction_rendering(
    result: str,
    detail: str | None,
    reason: str,
    event: str,
) -> None:
    notice = compaction_notice(
        result,  # type: ignore[arg-type]
        detail,
        SessionLabel(harness="claude", project="meridian-cli", tmux_session="dev"),
    )

    assert notice.title == f'[{reason}] dev: claude in project: "meridian-cli"'
    assert notice.body == event
    assert "You:" not in notice.body
    assert "Assistant:" not in notice.body


def test_label_fallbacks_and_rendered_notices_have_no_separator_glyph() -> None:
    labels = (
        SessionLabel(agent="coder", project="meridian-cli"),
        SessionLabel(harness="pi", work_id="idle-cache-notify"),
        SessionLabel(project="meridian-cli"),
        SessionLabel(),
    )
    assert [label.headline for label in labels] == [
        'coder in project: "meridian-cli"',
        'pi on work: "idle-cache-notify"',
        'Meridian in project: "meridian-cli"',
        "Meridian",
    ]

    notices = [
        stage_notice(
            stage,
            _state(last_user_text=None, last_assistant_text=None),
            label,
            include_messages=True,
            now_ms=1,
            warn_email=False,
        )
        for stage in ("push", "warn")
        for label in labels
    ]
    assert notices[0].body == "Your turn"
    assert notices[1].title == '[idle] pi on work: "idle-cache-notify"'
    assert notices[3].title == "[idle] Meridian"
    assert notices[7].title == "[cache warning] Meridian"
    assert all("·" not in notice.title and "·" not in notice.body for notice in notices)


@pytest.mark.parametrize(
    ("ttl", "anchor", "now", "estimated", "suffix"),
    [
        (3600, 0, 720_000, False, "48 min until cold"),
        (1800, 0, 720_000, True, "est. 18 min until cold"),
        (300, 0, 210_000, False, "2 min until cold"),
        (300, 0, 270_000, True, "est. <1 min until cold"),
        (300, 0, 300_000, False, "cache cold"),
        (300, 0, 360_000, True, "est. cache cold"),
        (None, 0, 720_000, True, None),
        (3600, None, 720_000, False, None),
    ],
)
def test_countdown_uses_current_time_and_persisted_ttl_confidence(
    ttl: int | None,
    anchor: int | None,
    now: int,
    estimated: bool,
    suffix: str | None,
) -> None:
    notice = stage_notice(
        "push",
        _state(ttl_seconds=ttl, ttl_estimated=estimated, idle_since_ms=anchor),
        SessionLabel(harness="pi", work_id="idle-cache-notify", tmux_session="a2"),
        include_messages=False,
        now_ms=now,
        warn_email=False,
    )
    expected = '[idle] a2: pi on work: "idle-cache-notify"'
    assert notice.title == expected + (f" — {suffix}" if suffix else "")


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
