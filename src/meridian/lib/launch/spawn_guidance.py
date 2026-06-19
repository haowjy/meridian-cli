"""Spawn-capability gating and harness-templated spawn usage contracts."""

from __future__ import annotations

from meridian.lib.catalog.agent import AgentProfile
from meridian.lib.harness.adapter import SpawnUsageContractVariants
from meridian.lib.launch.composition import (
    CONTEXT_ENV_BLOCK_NAME,
    CompositionBlock,
    GuidancePhase,
)

_WORK_DISCOVERY = """\
# Work coordination (meridian)

Group related spawns under a work item — shared dir, goal, session history.
Learn the commands:  meridian work -h"""

_SESSION_DISCOVERY = """\
# Session transcripts (meridian)

Read what past spawns did — full transcripts and progress logs, searchable.
Learn the commands:  meridian session -h"""


def build_spawn_usage_contract(variants: SpawnUsageContractVariants) -> str:
    """Render the shared spawn-usage contract filled with adapter-owned phrasing."""

    return f"""\
# Spawning subagents (meridian)

{variants.intro_line}

- Do NOT use the Agent() tool for work that a meridian agent can do.
  Use `meridian spawn -a <agent>` instead — it routes to the right
  model and harness, tracks the work, and produces inspectable artifacts.
- Launch with --bg; it returns a spawn id without waiting for the work and
  runs the worker detached:
      meridian spawn -a <agent> --prompt-file <prompt>.md --bg
- {variants.double_wrap_bullet}
- {variants.timeout_bullet}
- Track with no-arg wait — no id needed; it discovers your pending spawns
  by session and yields cache-cleanly. Re-invoke to keep waiting:
      meridian spawn wait
- Every --bg spawn must be drained with `meridian spawn wait` before you
  respond to the user, start dependent work, or end your turn; un-waited
  background spawns are lost.
- Full reference:  meridian spawn -h"""


def has_spawn_capability(profile: AgentProfile | None) -> bool:
    if profile is None:
        return False
    if profile.meridian_capabilities is not None and profile.meridian_capabilities.spawn is False:
        return False
    return len(profile.subagents) > 0


def build_guidance_blocks(
    *,
    profile: AgentProfile | None,
    spawn_usage_contract: str,
    bundle_inventory_prompt: str | None,
    context_prompt: str,
) -> tuple[CompositionBlock, ...]:
    """Build all system-prompt guidance blocks.

    Spawn-gated (only when has_spawn_capability): inventory + spawn contract.
    General (always): discovery pointers + context env vars.
    """
    blocks: list[CompositionBlock] = []
    if has_spawn_capability(profile):
        inventory = (bundle_inventory_prompt or "").strip()
        if inventory:
            blocks.append(CompositionBlock("inventory", GuidancePhase.GUIDANCE, 0, inventory))
        blocks.append(
            CompositionBlock(
                "spawn-contract",
                GuidancePhase.GUIDANCE,
                10,
                spawn_usage_contract,
            )
        )
    blocks.append(
        CompositionBlock("work-discovery", GuidancePhase.GUIDANCE, 20, _WORK_DISCOVERY)
    )
    blocks.append(
        CompositionBlock("session-discovery", GuidancePhase.GUIDANCE, 21, _SESSION_DISCOVERY)
    )
    context_env = (context_prompt or "").strip()
    if context_env:
        blocks.append(
            CompositionBlock(CONTEXT_ENV_BLOCK_NAME, GuidancePhase.ENVIRONMENT, 0, context_env)
        )
    return tuple(blocks)


__all__ = [
    "build_guidance_blocks",
    "build_spawn_usage_contract",
    "has_spawn_capability",
]
