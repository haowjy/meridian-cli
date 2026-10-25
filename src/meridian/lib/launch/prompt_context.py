"""Prompt context block producers for launch composition."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from meridian.lib.core.domain import SkillContent
from meridian.lib.launch.composition import PromptDocument


def _render_skill_block(skill: SkillContent, content: str) -> str:
    """Head a loaded skill so the agent knows `/<name>` is already in context.

    Skills loaded this way are often not model-invocable, so the harness's own
    skill list omits them; without the name, an agent told to follow `/<name>`
    concludes the skill is unavailable.
    """

    skill_dir = Path(skill.path).parent.as_posix()
    return (
        f"# Skill: /{skill.name}\n\n"
        f"Loaded: its full text follows, so `/{skill.name}` in your instructions "
        f"means this text. Its files are in `{skill_dir}/`; open the resources it "
        f"names from there.\n\n{content}"
    )


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
                content=_render_skill_block(skill, content),
                skill_type=skill.skill_type,
            )
        )
    return tuple(documents)


__all__ = [
    "compose_skill_prompt_documents",
]
