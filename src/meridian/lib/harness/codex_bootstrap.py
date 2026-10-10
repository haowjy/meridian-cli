"""Shared identity for Codex's Meridian-authored bootstrap turn."""

from __future__ import annotations

from typing import Final

BOOTSTRAP_TURN_PROMPT: Final[str] = "Meridian started"


def bootstrap_turn_prompt(agent_name: str | None) -> str:
    """Render the synthetic prompt used to materialize a fresh Codex rollout."""

    normalized_agent = (agent_name or "").strip()
    if normalized_agent:
        return f"{BOOTSTRAP_TURN_PROMPT} (agent: {normalized_agent})"
    return BOOTSTRAP_TURN_PROMPT


def is_bootstrap_turn_prompt(value: object) -> bool:
    """Return whether a notify input is Meridian's synthetic bootstrap prompt."""

    if value == BOOTSTRAP_TURN_PROMPT:
        return True
    if not isinstance(value, str):
        return False
    prefix = f"{BOOTSTRAP_TURN_PROMPT} (agent: "
    return value.startswith(prefix) and value.endswith(")") and bool(
        value[len(prefix) : -1].strip()
    )


__all__ = [
    "BOOTSTRAP_TURN_PROMPT",
    "bootstrap_turn_prompt",
    "is_bootstrap_turn_prompt",
]
