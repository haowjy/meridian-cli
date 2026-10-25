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
    "- Work item (tracked effort): `meridian work`.\n"
    "- Past sessions/transcripts: `meridian session search` with a ref, `--work`, "
    "`--workspace`, or `--global`; read with `meridian session log`.\n"
    "- Notify the human: `meridian notify`.\n"
    "- Validate Markdown links / inspect the link graph / validate Mermaid: "
    "`meridian kg check` / `meridian kg graph` / `meridian mermaid check`.\n"
    "- Hand off to a new primary: print, never run, a `meridian` command with concrete "
    "`--work`, `--task-dir`, `--prompt-file`, and the shown chat ID as parent `--from`; "
    "no variables.\n"
    "- Delegated agent access: an unsandboxed parent may relaunch via "
    "`meridian spawn --sandbox` / `--network`; a sandboxed parent cannot widen its floor."
)

_SPAWN_CAPABILITIES = (
    "- Delegate to an `@agent`: `meridian spawn`; attach skills `--skills`, pass files or a "
    "folder `-f`, carry a prior run `--from`, choose its checkout `--task-dir`, or override "
    "its model `-m`.\n"
    "- Agents you can use: `meridian spawn subagents`.\n"
    "- Live model catalog / model-specific prompting: `meridian mars models list` / "
    "`meridian mars models prompting`."
)

_SPAWN_PROMPTING = """\
# Prompting subagents

Every spawn starts with fresh context. Lead with the task, then give it what
you know and it doesn't: the decisions already made and why, the files and
artifacts that matter by path, the constraints, and what done looks like.
Pass a directory to show the shape of a tree, and name the few files whose
content matters most.

Point to where the work starts, and leave open what the spawn should judge
for itself: how far to look, how to work, and what else might be wrong. A
brief that lists the bugs to hunt or a range to stay inside gets back only
what you already suspected; name your suspicions as risks to check, beside
the open question. State what the spawn may edit.

Agent defaults fit most work. Say why before overriding the model: do so only
when the caller asks or the work needs more depth or a capability the defaults
lack. To get more depth, change the model, not the agent."""


def _build_capabilities_prompt(*, spawn_capable: bool) -> str:
    lines = [
        "# Meridian capabilities",
        "",
        "`meridian work path prompts/task.md` is a correct standalone call.",
        "Restricted shells refuse `cd ... && meridian ...`, pipes, `$()`, `VAR=1`, and "
        "`$VAR` expansion.",
        "Every command has `-h`; if one is refused, tell your caller—do not work "
        "around it.",
        "",
    ]
    if spawn_capable:
        lines.append(_SPAWN_CAPABILITIES)
    lines.append(_GENERAL_CAPABILITIES)
    return "\n".join(lines)


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
  background-wrapped. Do not pipe or truncate its output. Un-waited
  background spawns are lost.
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
    prompting judgment, and the spawn behavior contract. General capabilities
    and context paths are always present.
    """
    blocks: list[CompositionBlock] = []
    spawn_capable = has_spawn_capability(profile)
    if spawn_capable:
        inventory = (bundle_inventory_prompt or "").strip()
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
            CompositionBlock("spawn-prompting", GuidancePhase.GUIDANCE, 7, _SPAWN_PROMPTING)
        )
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
