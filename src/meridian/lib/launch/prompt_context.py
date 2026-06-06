"""Prompt context block producers for launch composition."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeGuard, cast

from meridian.lib.catalog.agent import AgentProfile, scan_agent_profiles
from meridian.lib.catalog.skill import split_markdown_frontmatter
from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import (
    ResolvedContextPaths,
    render_context_lines,
    resolve_context_paths,
)
from meridian.lib.core.domain import SkillContent
from meridian.lib.launch.composition import PromptDocument

_AGENT_INVENTORY_HEADING = "# Meridian Agents"


# ---------------------------------------------------------------------------
# Fallback agent inventory (mirrors mars launch-bundle inventory_prompt format)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentInventoryDisplay:
    """Launch-local view model for inventory prompt rendering."""

    name: str
    description: str
    mode: Literal["primary", "subagent"]
    model: str | None
    fanout: tuple[str, ...]


@dataclass(frozen=True)
class _ModelPolicyMatch:
    alias: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class _ModelPolicyEntry:
    match: _ModelPolicyMatch
    no_fallback: bool = False


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    if not isinstance(value, dict):
        return False
    return all(isinstance(key, str) for key in value)


def _optional_trimmed_str(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _parse_model_policy_match(value: object) -> _ModelPolicyMatch | None:
    if not _is_str_dict(value):
        return None
    return _ModelPolicyMatch(
        alias=_optional_trimmed_str(value.get("alias")),
        model=_optional_trimmed_str(value.get("model")),
    )


def _parse_model_policy_entry(value: object) -> _ModelPolicyEntry | None:
    if not _is_str_dict(value):
        return None
    match = _parse_model_policy_match(value.get("match"))
    if match is None:
        return None
    no_fallback = value.get("no-fallback") is True or value.get("no_fallback") is True
    return _ModelPolicyEntry(match=match, no_fallback=no_fallback)


def _parse_model_policy_entries(value: object) -> tuple[_ModelPolicyEntry, ...]:
    if not isinstance(value, list):
        return ()
    entries: list[_ModelPolicyEntry] = []
    for item in cast("list[object]", value):
        parsed = _parse_model_policy_entry(item)
        if parsed is not None:
            entries.append(parsed)
    return tuple(entries)


def _fanout_from_model_policies(policies: tuple[_ModelPolicyEntry, ...]) -> tuple[str, ...]:
    fanout: list[str] = []
    seen: set[str] = set()
    for entry in policies:
        if entry.no_fallback:
            continue
        match_value = entry.match.alias or entry.match.model
        if not match_value or match_value in seen:
            continue
        seen.add(match_value)
        fanout.append(match_value)
    return tuple(fanout)


def _inventory_display_from_profile(agent: AgentProfile) -> AgentInventoryDisplay:
    frontmatter, _ = split_markdown_frontmatter(agent.raw_content)
    model = _optional_trimmed_str(frontmatter.get("model"))
    policies = _parse_model_policy_entries(frontmatter.get("model-policies"))
    return AgentInventoryDisplay(
        name=agent.name,
        description=agent.description,
        mode=agent.mode,
        model=model,
        fanout=_fanout_from_model_policies(policies),
    )


def _read_native_agent_manifest(project_root: Path) -> dict[str, frozenset[str]]:
    """Read `.mars/native-agents.json` for harness-aware inventory partitioning."""

    manifest_path = project_root / ".mars" / "native-agents.json"
    try:
        content = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {}

    if not _is_str_dict(parsed) or parsed.get("version") != 1:
        return {}

    agents_raw = parsed.get("agents")
    if not _is_str_dict(agents_raw):
        return {}

    manifest: dict[str, frozenset[str]] = {}
    for name, harnesses in agents_raw.items():
        if not isinstance(harnesses, list):
            continue
        harness_set = frozenset(
            harness
            for harness in (_optional_trimmed_str(item) for item in cast("list[object]", harnesses))
            if harness is not None
        )
        if harness_set:
            manifest[name] = harness_set
    return manifest


def _render_spawn_guidance_header() -> tuple[str, ...]:
    return (
        "Write prompts to `/tmp/<name>.md`.",
        "Use `--bg` + `meridian spawn wait` for parallel work.",
        "Use `/handoff` when passing control back to the user.",
    )


def _render_native_section_heading(harness_id: str) -> tuple[str, ...]:
    if harness_id == "claude":
        return ("", "## Claude Agents (use `Agent({subagent_type: \"...\"})` tool)")
    return (
        "",
        "## Native Agents",
        "Use your native subagent tool for agents listed here.",
    )


def _render_meridian_agent_line(agent: AgentInventoryDisplay) -> str:
    description = agent.description.strip()
    if description:
        line = f"- `meridian spawn -a {agent.name}`: {description}"
    else:
        line = f"- `meridian spawn -a {agent.name}`"
    if agent.model:
        line += f" | Model: {agent.model}"
    if agent.fanout:
        line += f" | Fan-out: {', '.join(agent.fanout)}"
    return line


def _render_native_agent_line(agent: AgentInventoryDisplay) -> str:
    description = agent.description.strip()
    if description:
        return f"- {agent.name}: {description}"
    return f"- {agent.name}"


def _is_native_for_harness(
    agent_name: str,
    manifest: Mapping[str, frozenset[str]],
    harness_id: str | None,
) -> bool:
    if not harness_id:
        return False
    harnesses = manifest.get(agent_name)
    if not harnesses:
        return False
    return harness_id in harnesses


def _render_fallback_agent_inventory(
    *,
    agents: Sequence[AgentProfile],
    manifest: Mapping[str, frozenset[str]],
    harness_id: str | None,
) -> str | None:
    visible_agents = [agent for agent in agents if agent.model_invocable]
    if not visible_agents:
        return None

    use_native_partition = bool(harness_id and manifest)

    meridian_primary: list[AgentInventoryDisplay] = []
    meridian_subagent: list[AgentInventoryDisplay] = []
    native_agents: list[AgentInventoryDisplay] = []

    for agent in visible_agents:
        display = _inventory_display_from_profile(agent)
        if use_native_partition and _is_native_for_harness(agent.name, manifest, harness_id):
            native_agents.append(display)
        elif agent.mode == "primary":
            meridian_primary.append(display)
        else:
            meridian_subagent.append(display)

    lines = [_AGENT_INVENTORY_HEADING, "", *_render_spawn_guidance_header()]

    if meridian_subagent:
        lines.extend(["", "## Subagent"])
        for agent in meridian_subagent:
            lines.append(_render_meridian_agent_line(agent))

    if meridian_primary:
        lines.extend(["", "## Primary"])
        for agent in meridian_primary:
            lines.append(_render_meridian_agent_line(agent))

    if native_agents:
        lines.extend(_render_native_section_heading(harness_id or ""))
        for agent in native_agents:
            lines.append(_render_native_agent_line(agent))

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Skill and context prompt helpers
# ---------------------------------------------------------------------------


def _render_skill_blocks(skills: Sequence[SkillContent]) -> tuple[str, ...]:
    blocks: list[str] = []
    for skill in skills:
        content = skill.content.strip()
        if not content:
            continue
        blocks.append(f"# Skill: {Path(skill.path).as_posix()}\n\n{content}")
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

    blocks = _render_skill_blocks(skills)
    if not blocks:
        return None
    return _join_sections(blocks)


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
    active_work_dir: Path | None = None,
    include_inventory: bool = True,
    include_context: bool = True,
    harness_id: str | None = None,
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
            agents=agent_profiles,
            harness_id=harness_id,
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
    agents: list[AgentProfile] | None = None,
    harness_id: str | None = None,
) -> str | None:
    """Render installed agent inventory grouped by mode."""

    if agents is None:
        agents = sorted(
            scan_agent_profiles(project_root=project_root),
            key=lambda profile: profile.name,
        )

    if not agents:
        return None

    manifest = _read_native_agent_manifest(project_root)
    return _render_fallback_agent_inventory(
        agents=agents,
        manifest=manifest,
        harness_id=harness_id,
    )


__all__ = [
    "build_agent_inventory_prompt",
    "build_context_prompt",
    "build_launch_context_documents",
    "compose_skill_injections",
    "compose_skill_prompt_documents",
]
