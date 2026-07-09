"""Spawn-capability gating and harness-templated spawn usage contracts."""

from __future__ import annotations

from meridian.lib.catalog.agent import AgentProfile
from meridian.lib.harness.adapter import SpawnUsageContractVariants
from meridian.lib.launch.composition import (
    CONTEXT_ENV_BLOCK_NAME,
    CompositionBlock,
    GuidancePhase,
)

_SPAWN_PROMPTING = """\
# Prompting subagents

Every spawn starts with fresh context — scope the handoff to what the \
subagent needs, not what you've been thinking about. Front-load the task; \
the subagent should know its job by line 3.

Compose the lane at spawn time:
- `--skills skill1,skill2` attaches focus skills to any agent — prefer a
  near-fit agent plus an attached skill over a generic agent.
- `-m <model>` overrides the profile model — for deliberate fan-out (the
  same task across models) or when the lane needs a capability the
  default lacks; otherwise prefer the profile default.
- `--from <spawn-or-chat id>` hands the spawn a prior conversation's
  context — reach for it when the reasoning matters, not just the
  artifacts (knowledge capture, reviewing a decision trail).
- `--task-dir <path>` points the spawn's source edits at another
  checkout, e.g. a worktree lane that must not collide with others.

For model-specific prompting guidance:  meridian mars models prompting <agent-or-model>"""

_WORK_DISCOVERY = """\
# Work coordination (meridian)

Group related spawns under a work item — shared dir, goal, session history.
Learn the commands:  meridian work -h"""

_TASK_DIR_DISCOVERY = """\
# Source-edit directory (meridian)

`MERIDIAN_TASK_DIR` is where source reads, edits, git, builds, and tests run.
`MERIDIAN_ACTIVE_WORK_DIR` is for scratch/work artifacts — not your checkout.
Shell cwd may be the project/control root; `cd "$MERIDIAN_TASK_DIR"` or use absolute
paths before source ops.
Live query:  meridian task-dir"""

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
- {variants.double_wrap_bullet}
- {variants.timeout_bullet}
- Drain every --bg spawn with no-arg `meridian spawn wait` before you
  respond to the user, start dependent work, or end your turn. It finds
  your pending spawns by session; re-invoke to keep waiting. The wait
  itself can run in your native background execution to keep working
  while it blocks — it's the launch, not the wait, that must never be
  background-wrapped. Un-waited background spawns are lost.
- Playbook and full flags:  meridian spawn -h"""


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
            CompositionBlock("spawn-prompting", GuidancePhase.GUIDANCE, 5, _SPAWN_PROMPTING)
        )
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
        CompositionBlock("task-dir-discovery", GuidancePhase.GUIDANCE, 21, _TASK_DIR_DISCOVERY)
    )
    blocks.append(
        CompositionBlock("session-discovery", GuidancePhase.GUIDANCE, 22, _SESSION_DISCOVERY)
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
