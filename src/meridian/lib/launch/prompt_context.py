"""Prompt context block producers for launch composition."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from meridian.lib.core.domain import SkillContent, SkillDeliveryMode
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


def _named_skill_pointer(skill: SkillContent) -> str:
    """One line naming a loaded skill and the file that holds its body.

    The harness's own skill list omits non-model-invocable skills, so the
    `SKILL.md` path is what keeps a named skill reachable.
    """

    return f"Read `{Path(skill.path).as_posix()}` before using `/{skill.name}`."


def compose_skill_prompt_documents(
    skills: Sequence[SkillContent], skill_delivery: SkillDeliveryMode
) -> tuple[PromptDocument, ...]:
    """Format loaded skills as typed supplemental prompt documents.

    Named delivery turns each document into a pointer line, so the renderer groups
    and heads the skills the same way in both modes.
    """

    named = skill_delivery == "named"
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
                content=_named_skill_pointer(skill) if named else render_skill_block(skill),
                skill_type=skill.skill_type,
            )
        )
    return tuple(documents)


__all__ = [
    "compose_skill_prompt_documents",
    "render_skill_block",
]
