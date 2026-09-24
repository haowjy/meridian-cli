"""Dependency-light native Pi value mappings shared by admission and projection."""

from __future__ import annotations

_EFFORT_TO_THINKING: dict[str, str] = {
    "low": "minimal",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}


def project_pi_thinking_level(effort: str | None) -> str | None:
    """Return Pi's native thinking flag value for a supported Meridian effort."""
    return _EFFORT_TO_THINKING.get((effort or "").strip().lower())


__all__ = ["project_pi_thinking_level"]
