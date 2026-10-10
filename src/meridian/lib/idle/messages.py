"""Pure rendering for idle notifications."""

from __future__ import annotations

from typing import Literal

from meridian.lib.notify import Notice, SessionLabel
from meridian.lib.state.idle_store import CompactResultValue, IdleState


def _body(lines: list[str | None], *, fallback: str | None = None) -> str:
    rendered = [line for line in lines if line]
    if rendered:
        return "\n".join(rendered)
    return fallback or ""


def stage_notice(
    stage: Literal["push", "warn"],
    state: IdleState,
    label: SessionLabel,
    *,
    include_messages: bool,
    warn_minutes: int,
    warn_email: bool,
) -> Notice:
    """Build a waiting or cache-warning notice from persisted turn context."""

    event = f"Cache goes cold in {warn_minutes} min" if stage == "warn" else None
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
    return Notice(
        title=label.headline,
        body=_body(
            [event, user, assistant, label.footer],
            fallback="Your turn" if stage == "push" else None,
        ),
        priority=3 if stage == "push" else 4,
        email=warn_email if stage == "warn" else False,
        kind="idle",
    )


def compaction_notice(
    result: CompactResultValue,
    detail: str | None,
    state: IdleState,
    label: SessionLabel,
) -> Notice:
    """Build a compaction result notice without turn excerpts."""

    _ = state
    event = {
        "ok": "Compacted",
        "failed": "Compaction failed",
        "vetoed": "Compaction skipped",
    }[result]
    normalized_detail = detail.strip() if detail else ""
    if normalized_detail and result != "ok":
        event = f"{event}: {normalized_detail}"
    return Notice(
        title=label.headline,
        body=_body([event, label.footer]),
        priority=3 if result == "ok" else 4,
        email=False,
        kind="idle",
    )


__all__ = ["compaction_notice", "stage_notice"]
