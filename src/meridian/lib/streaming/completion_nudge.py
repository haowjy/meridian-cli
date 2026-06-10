"""Shared completion-nudge text and cadence for resident drains."""

from __future__ import annotations

COMPLETION_NUDGE_INTERVAL_SECONDS = 270.0
COMPLETION_NUDGE_MESSAGE = (
    "Are you done? Run `meridian spawn done` to finish, "
    "or `meridian spawn rearm` to keep going."
)
PI_COMPLETION_NUDGE_MESSAGE = "Are you done? Run `meridian spawn done` to finish."
TIMEOUT_SOON_COMPLETION_NUDGE_MESSAGE = (
    "This spawn times out soon. Run `meridian spawn rearm` to keep going, "
    "or `meridian spawn done` to finish."
)

__all__ = [
    "COMPLETION_NUDGE_INTERVAL_SECONDS",
    "COMPLETION_NUDGE_MESSAGE",
    "PI_COMPLETION_NUDGE_MESSAGE",
    "TIMEOUT_SOON_COMPLETION_NUDGE_MESSAGE",
]
