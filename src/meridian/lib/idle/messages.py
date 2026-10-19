"""Pure rendering for idle notifications."""

from __future__ import annotations

from typing import Literal

from meridian.lib.notify import SessionLabel
from meridian.lib.state.idle_store import IdleState
from meridian.plugin_api.types import HookEventName, Notification

# The hook event each stage's notification travels as.
STAGE_EVENTS: dict[Literal["push", "warn"], HookEventName] = {
    "push": "notify.your_turn",
    "warn": "notify.cache_expiring",
}


def _body(lines: list[str | None], *, fallback: str | None = None) -> str:
    rendered = [line for line in lines if line]
    if rendered:
        return "\n".join(rendered)
    return fallback or ""


def _cache_countdown(state: IdleState, now_ms: int) -> str | None:
    if state.ttl_seconds is None or state.idle_since_ms is None:
        return None
    remaining_ms = state.idle_since_ms + state.ttl_seconds * 1000 - now_ms
    if remaining_ms <= 0:
        countdown = "cache cold"
    elif remaining_ms < 60_000:
        countdown = "<1 min until cold"
    else:
        minutes = (remaining_ms + 59_999) // 60_000
        countdown = f"{minutes} min until cold"
    return f"est. {countdown}" if state.ttl_estimated else countdown


def stage_notice(
    stage: Literal["push", "warn"],
    state: IdleState,
    label: SessionLabel,
    *,
    include_messages: bool,
    now_ms: int,
    warn_email: bool,
) -> Notification:
    """Build a waiting or cache-warning notice from persisted turn context."""

    countdown = _cache_countdown(state, now_ms)
    title = f"[{'cache warning' if stage == 'warn' else 'idle'}] {label.headline}"
    if countdown is not None:
        title += f" — {countdown}"
    event = (
        f"{countdown or 'Cache expiry warning'}: reply to keep it warm"
        if stage == "warn"
        else None
    )
    user = (
        f"You: {state.last_user_text}"
        if include_messages and state.last_user_text
        else None
    )
    assistant = (
        f"Assistant: {state.last_assistant_text}"
        if include_messages and state.last_assistant_text
        else None
    )
    return Notification(
        title=title,
        body=_body(
            [event, user, assistant],
            fallback="Your turn" if stage == "push" else None,
        ),
        priority=3 if stage == "push" else 4,
        email=warn_email if stage == "warn" else False,
        label=label.to_dict(),
    )


__all__ = ["STAGE_EVENTS", "stage_notice"]
