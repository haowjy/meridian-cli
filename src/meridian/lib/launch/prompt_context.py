from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from meridian.lib.core.domain import SkillContent, SkillDeliveryMode
from meridian.lib.launch.composition import PromptDocument


def render_skill_block(skill: SkillContent) -> str:
    # Loaded skills may be absent from the harness's invocable skill list.
    skill_dir = Path(skill.path).parent.as_posix()
    return f"# Skill: /{skill.name} (loaded)\n\nFiles: `{skill_dir}/`\n\n{skill.content.strip()}"


def compose_skill_prompt_documents(
    skills: Sequence[SkillContent], skill_delivery: SkillDeliveryMode
) -> tuple[PromptDocument, ...]:
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
                # Guardrail skills have no later moment of use; request the read now.
                content=(
                    f"Read `{path}` now: `/{skill.name}` is loaded for this run."
                    if named
                    else render_skill_block(skill)
                ),
                skill_type=skill.skill_type,
            )
        )
    return tuple(documents)


__all__ = [
    "compose_skill_prompt_documents",
    "render_skill_block",
]
