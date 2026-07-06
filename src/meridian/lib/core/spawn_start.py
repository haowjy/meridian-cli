"""Creation-time spawn metadata shared across lifecycle/store start seams."""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot


@dataclass(frozen=True)
class SpawnStartMetadata:
    """User-facing metadata captured when a spawn row is first created."""

    desc: str | None = None
    work_id: str | None = None
    goal: str | None = None
    launch_policy_snapshot: LaunchPolicySnapshot | None = None


def compact_prompt_summary(prompt: str) -> str | None:
    """Return a single-line prompt summary capped at 50 characters."""

    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        return None
    compact_prompt = " ".join(normalized_prompt.split()).strip()
    if len(compact_prompt) <= 50:
        return compact_prompt
    return f"{compact_prompt[:47].rstrip()}..."


def derive_display_label(
    *,
    goal: str | None,
    desc: str | None,
    prompt: str,
) -> str | None:
    """Persist a prompt summary only when goal and desc are both absent."""

    if (goal or desc or "").strip():
        return None
    return compact_prompt_summary(prompt)


def resolve_spawn_display_label(
    goal: str | None,
    desc: str | None,
    display_label: str | None,
) -> str | None:
    """Resolve list/display text from persisted metadata only."""

    label = (goal or desc or display_label or "").strip()
    return label or None
