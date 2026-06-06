"""Prompt composition helpers for launch flows."""

import html
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from meridian.lib.catalog.agent import AgentProfile, scan_agent_profiles
from meridian.lib.catalog.skill import SkillRegistry
from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import (
    ResolvedContextPaths,
    render_context_lines,
    resolve_context_paths,
)
from meridian.lib.core.domain import SkillContent
from meridian.lib.launch.composition import PromptDocument

from .resolve import dedupe_skill_names

_AGENT_INVENTORY_HEADING = "# Meridian Agents"
_AGENT_DELEGATION_GUIDANCE_MARKER = (
    "meridian spawn -a <agent> --prompt-file /tmp/<file>.md"
)
_AGENT_DELEGATION_GUIDANCE = (
    "Use the Meridian agents list as a Meridian spawn menu, not as arbitrary "
    "harness-native agent choices.\n\n"
    "Prefer Meridian spawn for most subagent work: write the handoff to "
    "`/tmp/<file>.md`, then run "
    "`meridian spawn -a <agent> --prompt-file /tmp/<file>.md`. If launching "
    "with `--bg`, drain results with `meridian spawn wait`.\n\n"
    "Use Claude native `Agent` only when the task explicitly calls for a "
    "Claude-native agent/model or the user asks for Claude-specific delegation."
)


def dedupe_skill_contents(skills: Sequence[SkillContent]) -> tuple[SkillContent, ...]:
    """De-duplicate loaded skill payloads by skill name preserving order."""

    seen: set[str] = set()
    ordered: list[SkillContent] = []
    for skill in skills:
        if skill.name in seen:
            continue
        seen.add(skill.name)
        ordered.append(skill)
    return tuple(ordered)


def load_skill_contents(
    registry: SkillRegistry,
    names: Sequence[str],
    *,
    harness_id: str | None = None,
    selected_model_token: str | None = None,
    canonical_model_id: str | None = None,
) -> tuple[SkillContent, ...]:
    """Load skill contents in deterministic deduplicated order."""

    deduped_names = dedupe_skill_names(names)
    if not deduped_names:
        return ()
    loaded = registry.load(
        list(deduped_names),
        harness_id=harness_id,
        selected_model_token=selected_model_token,
        canonical_model_id=canonical_model_id,
    )
    return dedupe_skill_contents(loaded)


def build_report_instruction() -> str:
    """Build the report instruction appended to each composed run prompt."""

    return (
        "# Report\n\n"
        "**IMPORTANT - Your final assistant message must be the run report.**\n\n"
        "Provide a plain markdown report in your final assistant message.\n\n"
        "Include: what was done, key decisions made, files created/modified, "
        "verification results, and any issues or blockers."
    )


def build_goal_instruction(goal: str | None) -> str:
    """Render the deterministic spawn-goal completion contract block."""

    if goal is None:
        return ""
    if goal == "" or goal != goal.strip():
        raise ValueError("goal must be normalized before prompt rendering")
    escaped_goal = html.escape(goal, quote=False)
    return (
        "# Spawn Goal\n\n"
        "You have a completion contract for this spawn:\n\n"
        f"<goal>\n{escaped_goal}\n</goal>\n\n"
        "Work until the goal is complete. If the goal is impossible, unsafe, "
        "blocked by missing information or permissions, or disproportionate "
        "to continue, stop and report the blocker instead.\n\n"
        "When blocked, report what is blocked, the evidence observed, and the "
        "smallest next action or decision needed. Do not run forever or retry indefinitely."
    )


def build_spawn_preamble(launch_mode: str | None) -> str:
    """Render launch-mode behavioral framing for spawned subagents."""

    if launch_mode == "background":
        return (
            "# Session Context\n\n"
            "This is a **sub-agent session**. You are not talking to the user. "
            "Your final output is a structured report consumed by your parent agent. "
            "Work autonomously toward your objective. Only escalate if blocked."
        )
    if launch_mode == "foreground":
        return (
            "# Session Context\n\n"
            "This is a **sub-agent session**. You are not talking to the user. "
            "Your final output is a structured report consumed by your parent agent."
        )
    return ""


def build_primary_preamble() -> str:
    """Render session-context framing for primary (user-facing) sessions."""

    return (
        "# Session Context\n\n"
        "This is a **primary session**. You are talking directly to the user."
    )


def build_work_goal_instruction(work_goal: str | None) -> str:
    if work_goal is None:
        return ""
    if work_goal == "" or work_goal != work_goal.strip():
        raise ValueError("work goal must be normalized before prompt rendering")
    escaped = html.escape(work_goal, quote=False)
    return (
        "# Goal of Your Work\n\n"
        f"<work-goal>\n{escaped}\n</work-goal>\n\n"
        "This is the overarching goal of the work item you are contributing to. "
        "Your specific task may be narrower, but keep this broader goal in mind."
    )


def _render_skill_blocks(skills: Sequence[SkillContent]) -> tuple[str, ...]:
    blocks: list[str] = []
    for skill in skills:
        content = skill.content.strip()
        if not content:
            continue
        blocks.append(f"# Skill: {skill.name}\n\n{content}")
    return tuple(blocks)


def _join_sections(sections: Sequence[str]) -> str:
    non_empty = [section.strip() for section in sections if section.strip()]
    return "\n\n".join(non_empty)



def compose_skill_prompt_documents(skills: Sequence[SkillContent]) -> tuple[PromptDocument, ...]:
    """Format loaded skills as typed supplemental prompt documents."""

    documents: list[PromptDocument] = []
    for skill in skills:
        content = skill.content.strip()
        if not content:
            continue
        path = Path(skill.path).as_posix()
        documents.append(
            PromptDocument(
                kind="skill",
                logical_name=skill.name,
                path=path,
                content=f"# Skill: {path}\n\n{content}",
                skill_type=skill.skill_type,
            )
        )
    return tuple(documents)


def compose_skill_injections(skills: Sequence[SkillContent]) -> str | None:
    """Format skill content for --append-system-prompt injection.

    Includes full skill filepath and content (not frontmatter).
    Returns None when there are no skills (caller omits the flag entirely).
    """

    blocks: list[str] = []
    for skill in skills:
        content = skill.content.strip()
        if not content:
            continue
        blocks.append(f"# Skill: {Path(skill.path).as_posix()}\n\n{content}")

    if not blocks:
        return None
    return _join_sections(blocks)


def _render_agent_line(agent: AgentProfile) -> str:
    description = agent.description.strip()
    return f"- {agent.name}: {description}" if description else f"- {agent.name}"


def with_agent_inventory_guidance(inventory_prompt: str) -> str:
    """Ensure delegation guidance leads into the Meridian Agents inventory."""

    prompt = inventory_prompt.strip()
    if not prompt:
        return prompt

    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == _AGENT_INVENTORY_HEADING:
            next_section_index = next(
                (
                    offset
                    for offset, candidate in enumerate(lines[index + 1 :], start=index + 1)
                    if candidate.startswith("## ")
                ),
                len(lines),
            )
            heading_lead_in = "\n".join(lines[index + 1 : next_section_index])
            if _AGENT_DELEGATION_GUIDANCE_MARKER in heading_lead_in:
                return prompt
            insert_at = index + 1
            if insert_at < len(lines) and not lines[insert_at].strip():
                insert_at += 1
            return "\n".join(
                (
                    *lines[:insert_at],
                    _AGENT_DELEGATION_GUIDANCE,
                    "",
                    *lines[insert_at:],
                )
            ).strip()
    return prompt


def build_context_prompt(
    *,
    project_root: Path,
    active_work_dir: Path | None = None,
    context_config: ContextConfig | None = None,
    resolved_context: ResolvedContextPaths | None = None,
) -> str | None:
    """Render resolved context paths for launch system context.

    Produces a block showing available context directories and their
    env var names so agents can reference them directly.
    Returns None when no context is resolvable.
    """

    if resolved_context is None:
        if context_config is None:
            from meridian.lib.state.paths import load_context_config

            context_config = load_context_config(project_root) or ContextConfig()
        resolved_context = resolve_context_paths(project_root, context_config)

    header = [
        "# Meridian Context",
        "",
        "Resolved context directories available via environment variables.",
        "",
    ]
    context_lines = render_context_lines(
        resolved_context,
        check_env=False,
        active_work_dir=active_work_dir,
    )

    return "\n".join([*header, *context_lines]).strip()


def build_launch_context_documents(
    *,
    project_root: Path,
    alias_catalog: Mapping[str, Any] | None = None,
    active_work_dir: Path | None = None,
    include_inventory: bool = True,
    include_context: bool = True,
) -> tuple[str | None, str | None]:
    """Resolve inventory/context prompt documents for launch composition."""

    agent_inventory_prompt: str | None = None
    context_prompt: str | None = None

    if include_inventory:
        agent_profiles = sorted(
            scan_agent_profiles(project_root=project_root),
            key=lambda profile: profile.name,
        )
        agent_inventory_prompt = build_agent_inventory_prompt(
            project_root=project_root,
            alias_catalog=dict(alias_catalog) if alias_catalog is not None else None,
            agents=agent_profiles,
        )

    if include_context:
        context_prompt = build_context_prompt(
            project_root=project_root,
            active_work_dir=active_work_dir,
        )

    return agent_inventory_prompt, context_prompt


def build_agent_inventory_prompt(
    *,
    project_root: Path,
    alias_catalog: dict[str, Any] | None = None,
    agents: list[AgentProfile] | None = None,
) -> str | None:
    """Render installed agent inventory grouped by mode."""

    if agents is None:
        agents = sorted(
            scan_agent_profiles(project_root=project_root),
            key=lambda profile: profile.name,
        )

    if not agents:
        return None

    visible_agents = [agent for agent in agents if agent.model_invocable]
    if not visible_agents:
        return None

    _ = alias_catalog

    lines = [
        _AGENT_INVENTORY_HEADING,
        "",
        "Installed Meridian agents available at launch time.",
    ]

    primary_agents = [agent for agent in visible_agents if agent.mode == "primary"]
    subagent_agents = [agent for agent in visible_agents if agent.mode != "primary"]

    if primary_agents:
        lines.extend(["", "## Primary"])
        for agent in primary_agents:
            lines.append(_render_agent_line(agent))

    if subagent_agents:
        lines.extend(["", "## Subagent"])
        for agent in subagent_agents:
            lines.append(_render_agent_line(agent))

    return with_agent_inventory_guidance("\n".join(lines))



__all__ = [
    "build_context_prompt",
    "build_goal_instruction",
    "build_launch_context_documents",
    "build_primary_preamble",
    "build_report_instruction",
    "build_spawn_preamble",
    "build_work_goal_instruction",
    "compose_skill_injections",
    "compose_skill_prompt_documents",
    "dedupe_skill_contents",
    "dedupe_skill_names",
    "load_skill_contents",
]
