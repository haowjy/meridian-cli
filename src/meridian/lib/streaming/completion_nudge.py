"""Shared completion-nudge text and cadence for resident drains."""

from __future__ import annotations

COMPLETION_NUDGE_INTERVAL_SECONDS = 270.0
PI_COMPLETION_NUDGE_MESSAGE = "Are you done? Run `meridian spawn done` to finish."

__all__ = [
    "COMPLETION_NUDGE_INTERVAL_SECONDS",
    "PI_COMPLETION_NUDGE_MESSAGE",
]
