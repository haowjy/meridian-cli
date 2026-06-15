"""Spawn-capability gating and harness-templated spawn usage contracts."""

from __future__ import annotations

from meridian.lib.catalog.agent import AgentProfile
from meridian.lib.core.types import HarnessId
from meridian.lib.launch.composition import CompositionBlock, GuidancePhase

_CLAUDE_SPAWN_CONTRACT = """\
# Spawning subagents (meridian)

Launch detached, then wait — never block the turn or background-wrap.

- Launch with --bg; it returns a spawn id without waiting for the work and
  runs the worker detached:
      meridian spawn -a <agent> --prompt-file <prompt>.md --bg
- NEVER wrap `meridian spawn --bg` inside Bash's run_in_background. It
  already detaches — double-wrapping adds nothing and lets the harness kill
  the launcher mid-startup, before it hands off to the detached worker; the
  spawn then fails (recorded and visible, but the work never runs).
- NEVER block-foreground a long spawn (omitting --bg). It will outlive your
  Bash command timeout; you lose the thread while the spawn runs on.
- Track with no-arg wait — no id needed; it discovers your pending spawns
  by session and yields cache-cleanly. Re-invoke to keep waiting:
      meridian spawn wait
- Every --bg spawn must be drained with `meridian spawn wait` before you
  respond to the user, start dependent work, or end your turn; un-waited
  background spawns are lost.
- Full reference:  meridian spawn -h"""

_GENERIC_SPAWN_CONTRACT = """\
# Spawning subagents (meridian)

Launch detached, then wait — never block the turn or double-background.

- Launch with --bg; it returns a spawn id without waiting for the work and
  runs the worker detached:
      meridian spawn -a <agent> --prompt-file <prompt>.md --bg
- NEVER wrap `meridian spawn --bg` in your harness's background execution.
  It already detaches — double-wrapping adds nothing and lets the harness
  kill the launcher mid-startup, before it hands off to the detached worker;
  the spawn then fails (recorded and visible, but the work never runs).
- NEVER block-foreground a long spawn (omitting --bg). It will outlive your
  command timeout; you lose the thread while the spawn runs on.
- Track with no-arg wait — no id needed; it discovers your pending spawns
  by session and yields cache-cleanly. Re-invoke to keep waiting:
      meridian spawn wait
- Every --bg spawn must be drained with `meridian spawn wait` before you
  respond to the user, start dependent work, or end your turn; un-waited
  background spawns are lost.
- Full reference:  meridian spawn -h"""

_WORK_DISCOVERY = """\
# Work coordination (meridian)

Group related spawns under a work item — shared dir, goal, session history.
Learn the commands:  meridian work -h"""

_SESSION_DISCOVERY = """\
# Session transcripts (meridian)

Read what past spawns did — full transcripts and progress logs, searchable.
Learn the commands:  meridian session -h"""


def _spawn_usage_contract(harness: HarnessId) -> str:
    if harness == HarnessId.CLAUDE:
        return _CLAUDE_SPAWN_CONTRACT
    return _GENERIC_SPAWN_CONTRACT


def has_spawn_capability(profile: AgentProfile | None) -> bool:
    if profile is None:
        return False
    if profile.meridian_capabilities is not None and profile.meridian_capabilities.spawn is False:
        return False
    return len(profile.subagents) > 0


def build_guidance_blocks(
    *,
    profile: AgentProfile | None,
    harness_id: HarnessId,
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
                _spawn_usage_contract(harness_id),
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
        blocks.append(CompositionBlock("context-env", GuidancePhase.ENVIRONMENT, 0, context_env))
    return tuple(blocks)


__all__ = [
    "build_guidance_blocks",
    "has_spawn_capability",
]
