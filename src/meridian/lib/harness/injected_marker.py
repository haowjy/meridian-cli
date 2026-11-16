"""The marker on every user-turn message Meridian itself injects into a session.

Harnesses tag only what they write; text Meridian sends in through stdin, a queue or a
control socket reaches the transcript as a plain user message. Every producer builds its
text with `marked`, and the transcript normalizer reads it back with `is_marked`.
"""

from __future__ import annotations

MARKER = "[Meridian]"

# Fixed openings of what builds before the marker existed, so older transcripts classify too.
_LEGACY_PREFIXES = (
    "Spawns you started have finished",
    "This spawn has gone idle.",
    "Descendant spawns are still running",
    "Background tasks are still running",
    "Background tasks finished, but",
    "Before this spawn closes, confirm",
    "If you are finished, run `meridian spawn done`; your reply to this message",
)


def marked(text: str) -> str:
    return text if text.startswith(MARKER) else f"{MARKER} {text}"


def is_marked(text: str) -> bool:
    return text.lstrip().startswith((MARKER, *_LEGACY_PREFIXES))
