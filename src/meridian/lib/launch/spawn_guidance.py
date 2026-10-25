"""Capability guidance and harness-templated spawn behavior contracts."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.bundled_skills import list_bundled_skills
from meridian.lib.catalog.agent import AgentProfile
from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import (
    ResolvedContextPaths,
    context_env_key,
    resolve_context_paths,
)
from meridian.lib.harness.adapter import SpawnUsageContractVariants
from meridian.lib.launch.composition import (
    CAPABILITIES_BLOCK_NAME,
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
    "- Project guidance graph: `meridian qi graph`."
)

_SPAWN_CAPABILITIES = (
    "- Delegate to an `@agent`: `meridian spawn`; write the brief under the work directory's "
    "`prompts/` with `meridian work path`, pass its absolute path to `--prompt-file`, attach "
    "skills with `--skills`, pass files or a folder with `-f`, carry a prior run with "
    "`--from`, choose its checkout with `--task-dir`, or override its model with `-m`.\n"
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

def _build_meridian_skills_prompt(*, spawn_capable: bool) -> str:
    lines = ["# Meridian skills", ""]
    for skill in list_bundled_skills():
        if skill.spawn_only and not spawn_capable:
            continue
        description = skill.description
        if description.startswith("When "):
            description = "when " + description.removeprefix("When ")
        lines.append(
            f"- {skill.name}: {description} Read `meridian skills {skill.name}`."
        )
    return "\n".join(lines)


def build_capabilities_prompt(
    *,
    spawn_capable: bool,
    project_root: Path,
    control_root: Path,
    task_dir: Path,
    active_work_dir: Path | None = None,
    spawn_id: str | None = None,
    chat_id: str | None = None,
    work_id: str | None = None,
    context_config: ContextConfig | None = None,
    resolved_context: ResolvedContextPaths | None = None,
) -> str:
    """Render the single late-bound capability and directory guidance block."""

    if resolved_context is None:
        if context_config is None:
            from meridian.lib.state.paths import load_context_config

            context_config = load_context_config(project_root) or ContextConfig()
        resolved_context = resolve_context_paths(project_root, context_config)

    lines = [
        "# Meridian capabilities",
        "",
        "Run each Meridian command as its own shell call, for example `meridian task-dir`.",
        "Restricted shells refuse `cd ... && meridian ...`, pipes, `$()`, `VAR=1`, and "
        "`$VAR` expansion.",
        "Every command has `-h`; if one is refused, tell your caller—do not work "
        "around it.",
        "",
    ]
    if spawn_capable:
        lines.append(_SPAWN_CAPABILITIES)
    lines.append(_GENERAL_CAPABILITIES)

    if active_work_dir is None:
        lines.append(
            "- No active work directory (`$MERIDIAN_ACTIVE_WORK_DIR`); inspect: "
            "`meridian work current`."
        )
    else:
        item = f" for work item `{work_id}`" if (work_id or "").strip() else ""
        lines.append(
            f"- `{active_work_dir.as_posix()}` — work directory for scratch/artifacts "
            f"(`$MERIDIAN_ACTIVE_WORK_DIR`{item}); inspect: `meridian work current`."
        )

    task_path = task_dir.as_posix()
    control_path = control_root.as_posix()
    spawn_suffix = (
        "; set the work item's checkout with `meridian work task-dir`, or one spawn's "
        "checkout with `meridian spawn --task-dir`"
        if spawn_capable
        else ""
    )
    if task_dir == control_root:
        lines.append(
            f"- `{task_path}` — source checkout and shell cwd "
            "(`$MERIDIAN_TASK_DIR`, `$MERIDIAN_PROJECT_DIR`); inspect: `meridian task-dir`"
            f"{spawn_suffix}."
        )
    else:
        lines.append(
            f"- `{task_path}` — source checkout (`$MERIDIAN_TASK_DIR`); shell cwd is "
            f"`{control_path}` (`$MERIDIAN_PROJECT_DIR`), so use absolute paths; inspect: "
            f"`meridian task-dir`{spawn_suffix}."
        )

    if resolved_context.kb_root is None:
        lines.append(
            "- No resolved knowledge base (`$MERIDIAN_CONTEXT_KB_DIR`); inspect: "
            "`meridian context kb`."
        )
    else:
        lines.append(
            f"- `{resolved_context.kb_root.as_posix()}` — durable knowledge base "
            "(`$MERIDIAN_CONTEXT_KB_DIR`); inspect: `meridian context kb`."
        )

    run_id = (spawn_id or "").strip()
    if run_id:
        lines.append(
            f"- `{run_id}` — your run ID (`$MERIDIAN_SPAWN_ID`), the ID your caller "
            "holds; use it in names."
        )
    current_chat_id = (chat_id or "").strip()
    if current_chat_id:
        lines.append(
            f"- `{current_chat_id}` — root conversation ID (`$MERIDIAN_CHAT_ID`), parent "
            "for handoff `--from`."
        )

    other_context: list[str] = []
    if resolved_context.work_archive is not None:
        other_context.append(
            f"`{resolved_context.work_archive.as_posix()}` — work archive "
            f"(`${context_env_key('work_archive')}`)"
        )
    for name in sorted(resolved_context.extra):
        path, _ = resolved_context.extra[name]
        other_context.append(
            f"`{path.as_posix()}` — {name} (`${context_env_key(name)}`)"
        )
    if other_context:
        lines.append("- " + "; ".join(other_context) + "; inspect: `meridian context`.")

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
    harness_guidance: str | None = None,
) -> tuple[CompositionBlock, ...]:
    """Build all system-prompt guidance blocks.

    Spawn-gated (only when has_spawn_capability): inventory, spawn capabilities,
    prompting judgment, and the spawn behavior contract. General capabilities
    and context paths are always present. The empty capability block is filled at bind,
    after its directory and run identity facts are known.
    """
    blocks: list[CompositionBlock] = []
    spawn_capable = has_spawn_capability(profile)
    if spawn_capable:
        inventory = (bundle_inventory_prompt or "").strip()
        if inventory:
            blocks.append(CompositionBlock("inventory", GuidancePhase.GUIDANCE, 0, inventory))
    blocks.append(
        CompositionBlock(
            "meridian-skills",
            GuidancePhase.GUIDANCE,
            4,
            _build_meridian_skills_prompt(spawn_capable=spawn_capable),
        )
    )
    blocks.append(
        CompositionBlock(
            CAPABILITIES_BLOCK_NAME,
            GuidancePhase.GUIDANCE,
            5,
            "",
        )
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
    "build_capabilities_prompt",
    "build_guidance_blocks",
    "build_spawn_usage_contract",
    "has_spawn_capability",
]
