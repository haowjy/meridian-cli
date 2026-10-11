"""Normalize short, plain-text notification excerpts."""

from __future__ import annotations

import re

USER_EXCERPT_CHARS = 120
ASSISTANT_EXCERPT_CHARS = 280

_SYSTEM_REMINDER = re.compile(
    r"<system-reminder\b[^>]*>.*?</system-reminder\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_UNCLOSED_SYSTEM_REMINDER = re.compile(
    r"<system-reminder\b[^>]*>.*$",
    flags=re.IGNORECASE | re.DOTALL,
)
_WHITESPACE = re.compile(r"\s+")


def _without_system_reminders(text: str) -> str:
    return _UNCLOSED_SYSTEM_REMINDER.sub(" ", _SYSTEM_REMINDER.sub(" ", text))


def trim_excerpt(text: str | None, limit: int, *, user: bool = False) -> str | None:
    """Collapse whitespace and shorten one excerpt at a word boundary."""

    if text is None:
        return None
    source = _without_system_reminders(text) if user else text
    normalized = _WHITESPACE.sub(" ", source).strip()
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    prefix = normalized[: limit - 1]
    boundary = prefix.rfind(" ")
    if boundary > 0:
        prefix = prefix[:boundary]
    return prefix.rstrip() + "…"


def trim_turn_excerpts(
    user_text: str | None,
    assistant_text: str | None,
) -> tuple[str | None, str | None]:
    """Normalize the two excerpts stored with an idle turn."""

    return (
        trim_excerpt(user_text, USER_EXCERPT_CHARS, user=True),
        trim_excerpt(assistant_text, ASSISTANT_EXCERPT_CHARS),
    )


__all__ = [
    "ASSISTANT_EXCERPT_CHARS",
    "USER_EXCERPT_CHARS",
    "trim_excerpt",
    "trim_turn_excerpts",
]
