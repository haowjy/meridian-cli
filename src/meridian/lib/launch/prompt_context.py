"""Prompt context block producers for launch composition."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import (
    ResolvedContextPaths,
    render_context_lines,
    resolve_context_paths,
)
from meridian.lib.core.domain import SkillContent
from meridian.lib.launch.composition import PromptDocument

CONTEXT_PROMPT_HEADER = "# Meridian Context"
CONTEXT_PROMPT_FOOTER = "Inspect or configure: meridian context -h"


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
        CONTEXT_PROMPT_HEADER,
        "",
        "Resolved context directories available via environment variables.",
        "",
    ]
    context_lines = render_context_lines(
        resolved_context,
        check_env=False,
        active_work_dir=active_work_dir,
    )

    footer = ["", CONTEXT_PROMPT_FOOTER]
    return "\n".join([*header, *context_lines, *footer]).strip()


__all__ = [
    "CONTEXT_PROMPT_FOOTER",
    "CONTEXT_PROMPT_HEADER",
    "build_context_prompt",
    "compose_skill_injections",
    "compose_skill_prompt_documents",
]
