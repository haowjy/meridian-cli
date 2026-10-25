"""Capability guidance and harness-templated spawn behavior contracts."""

from __future__ import annotations

from meridian.lib.catalog.agent import AgentProfile
from meridian.lib.harness.adapter import SpawnUsageContractVariants
from meridian.lib.launch.composition import (
    CONTEXT_ENV_BLOCK_NAME,
    CompositionBlock,
    GuidancePhase,
)

_GENERAL_CAPABILITIES = (
    "- Work item (tracked effort): `meridian work` (`meridian work -h`).\n"
    "- Past sessions and transcripts: `meridian session` (`meridian session -h`).\n"
    "- Notify the human: `meridian notify` (`meridian notify -h`).\n"
    "- Validate Markdown links / Mermaid diagrams: `meridian kg check` / "
    "`meridian mermaid check` (`meridian kg check -h`; `meridian mermaid check -h`).\n"
    "- Hand off to a new primary session: `meridian` (`meridian --mode human -h`).\n"
    "- If a delegated agent lacks access: a sandboxed parent cannot widen its "
    "sandbox/network floor; otherwise relaunch with `--sandbox` / `--network` on "
    "`meridian spawn` (`meridian spawn -h`)."
)

_SPAWN_CAPABILITIES = (
    "- Delegate to an `@agent`: `meridian spawn` (`meridian spawn -h`).\n"
    "- Agents you can use: `meridian spawn subagents` "
    "(`meridian spawn subagents -h`).\n"
    "- Prompt subagents with the task first, then decisions, paths, constraints, done "
    "criteria, and edit scope; leave method and wider risks open: `meridian spawn` "
    "(`meridian spawn -h`).\n"
    "- Live model catalog / model-specific prompting: `meridian mars models list` / "
    "`meridian mars models prompting` (`meridian mars models list -h`; "
    "`meridian mars models prompting -h`)."
)


def _build_capabilities_prompt(*, spawn_capable: bool) -> str:
    lines = [
        "# Meridian capabilities",
        "",
        "Run `meridian` commands as standalone shell calls (no `cd ... &&`, pipes, "
        "`$()`, or `VAR=1`).",
        "",
    ]
    if spawn_capable:
        lines.append(_SPAWN_CAPABILITIES)
    lines.append(_GENERAL_CAPABILITIES)
    return "\n".join(lines)


_DUPLICATE_INVENTORY_TIPS = frozenset(
    {
        "Write prompts to `$(meridian work path prompts/<name>.md)`.",
        "Use `--bg` + `meridian spawn wait` for parallel work.",
    }
)


def _inventory_without_duplicate_tips(inventory_prompt: str) -> str:
    """Remove only bundle tips now owned by capability and spawn-rule guidance."""

    lines = inventory_prompt.strip().splitlines()
    return "\n".join(line for line in lines if line not in _DUPLICATE_INVENTORY_TIPS).strip()


def build_spawn_usage_contract(variants: SpawnUsageContractVariants) -> str:
    """Render the behavioral spawn contract filled with adapter-owned phrasing."""

    return f"""\
# Meridian spawn rules

- Use Meridian, not the harness's native agent tool, when a listed Meridian agent fits.
- {variants.double_wrap_bullet}
- {variants.timeout_bullet}
- Drain every --bg spawn with no-arg `meridian spawn wait` before you
  respond to the user, start dependent work, or end your turn. It finds
  running and owed spawns by session; re-invoke to keep waiting. The wait
  itself can run in your native background execution to keep working
  while it blocks — it's the launch, not the wait, that must never be
  background-wrapped. Un-waited background spawns are lost.
- Before a child closes, Meridian may ask it to confirm completion; follow the
  confirmation message's instructions."""


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
    harness_guidance: str | None = None,
) -> tuple[CompositionBlock, ...]:
    """Build all system-prompt guidance blocks.

    Spawn-gated (only when has_spawn_capability): inventory, spawn capabilities,
    and the spawn behavior contract. General capabilities and context paths are
    always present.
    """
    blocks: list[CompositionBlock] = []
    spawn_capable = has_spawn_capability(profile)
    if spawn_capable:
        inventory = _inventory_without_duplicate_tips(bundle_inventory_prompt or "")
        if inventory:
            blocks.append(CompositionBlock("inventory", GuidancePhase.GUIDANCE, 0, inventory))
    blocks.append(
        CompositionBlock(
            "capabilities",
            GuidancePhase.GUIDANCE,
            5,
            _build_capabilities_prompt(spawn_capable=spawn_capable),
        )
    )
    context_env = (context_prompt or "").strip()
    if context_env:
        blocks.append(
            CompositionBlock(CONTEXT_ENV_BLOCK_NAME, GuidancePhase.GUIDANCE, 6, context_env)
        )
    if spawn_capable:
        blocks.append(
            CompositionBlock(
                "spawn-contract",
                GuidancePhase.GUIDANCE,
                10,
                spawn_usage_contract,
            )
        )
    harness_hint = (harness_guidance or "").strip()
    if harness_hint:
        blocks.append(
            CompositionBlock("harness-guidance", GuidancePhase.GUIDANCE, 23, harness_hint)
        )
    return tuple(blocks)


__all__ = [
    "build_guidance_blocks",
    "build_spawn_usage_contract",
    "has_spawn_capability",
]
