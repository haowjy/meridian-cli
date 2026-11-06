from __future__ import annotations

from meridian.lib.idle.messages import stage_notice
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

    assert push.title == "[idle] main: tech-lead (codex)"
    assert push.body == (
        "You: can u first test/install it locally?\n"
        "Assistant: Your installed meridian is exactly the PR's latest code.\n"
        'main: tech-lead (codex) on work: "idle-cache-notify"'
    )
    assert warn.title == "[cache warning] main: tech-lead (codex)"
    assert warn.body.startswith("Cache expiry warning: reply to keep it warm\nYou: ")
    assert (hidden.title, hidden.body, hidden.priority, hidden.email) == (
        "[idle] coder",
        'Your turn\ncoder in project: "meridian-cli"',
        3,
        False,
    )

    fallbacks = (
        SessionLabel(agent="coder", project="meridian-cli"),
        SessionLabel(harness="pi", work_id="idle-cache-notify"),
        SessionLabel(project="meridian-cli"),
        SessionLabel(),
    )
    assert [fallback.headline for fallback in fallbacks] == [
        'coder in project: "meridian-cli"',
        'pi on work: "idle-cache-notify"',
        'Meridian in project: "meridian-cli"',
        "Meridian",
    ]
    bare = stage_notice(
        "warn",
        _state(last_user_text=None, last_assistant_text=None),
        fallbacks[-1],
        include_messages=True,
        now_ms=1,
        warn_email=False,
    )
    assert bare.body == "Cache expiry warning: reply to keep it warm\nMeridian"


