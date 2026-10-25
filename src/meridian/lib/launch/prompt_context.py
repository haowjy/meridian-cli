"""Prompt context block producers for launch composition."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from meridian.lib.core.domain import SkillContent
from meridian.lib.launch.composition import PromptDocument


def render_skill_block(skill: SkillContent) -> str:
    """Head a loaded skill so the agent knows `/<name>` is already in context.

    Skills loaded this way are often not model-invocable, so the harness's own
    skill list omits them; without the name, an agent told to follow `/<name>`
    concludes the skill is unavailable. `meridian skills <name>` prints bundled
    skills through this same block, so reading one looks like activating it.
    """

    skill_dir = Path(skill.path).parent.as_posix()
    return (
        f"# Skill: /{skill.name} (loaded)\n\n"
        f"Files: `{skill_dir}/`\n\n{skill.content.strip()}"
    )


def compose_skill_prompt_documents(skills: Sequence[SkillContent]) -> tuple[PromptDocument, ...]:
    """Format loaded skills as typed supplemental prompt documents."""

    documents: list[PromptDocument] = []
    for skill in skills:
        if not skill.content.strip():
            continue
        path = Path(skill.path).as_posix()
        documents.append(
            PromptDocument(
                kind="skill",
                logical_name=skill.name,
                path=path,
                content=render_skill_block(skill),
                skill_type=skill.skill_type,
            )
        )
    return tuple(documents)


__all__ = [
    "compose_skill_prompt_documents",
    "render_skill_block",
]
