"""Prompt context block producers for launch composition."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import (
    ResolvedContextPaths,
    context_env_key,
    resolve_context_paths,
)
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


def build_context_prompt(
    *,
    project_root: Path,
    task_dir: Path,
    active_work_dir: Path | None = None,
    context_config: ContextConfig | None = None,
    resolved_context: ResolvedContextPaths | None = None,
) -> str:
    """Render late-bound directory capability lines for launch guidance.

    Each source of launch state appears once: as the environment variable the
    agent should use, its resolved value, the command that inspects it, and the
    corresponding help command.
    """

    if resolved_context is None:
        if context_config is None:
            from meridian.lib.state.paths import load_context_config

            context_config = load_context_config(project_root) or ContextConfig()
        resolved_context = resolve_context_paths(project_root, context_config)

    work_value = (
        f" ({active_work_dir.as_posix()})" if active_work_dir is not None else " (not active)"
    )
    lines = [
        "- Work directory (scratch/artifacts for this work): "
        f"`$MERIDIAN_ACTIVE_WORK_DIR`{work_value}; locate it with "
        "`meridian work current`.",
        "- Source checkout (reads, edits, git, builds, tests): "
        f"`$MERIDIAN_TASK_DIR` ({task_dir.as_posix()}); shell cwd may be "
        "`$MERIDIAN_PROJECT_DIR`, so `cd \"$MERIDIAN_TASK_DIR\"` or use absolute paths; "
        "inspect with `meridian task-dir`, and select it with `meridian spawn --task-dir` "
        "or `meridian --mode human --task-dir`.",
    ]

    if resolved_context.kb_root is None:
        kb_value = " (unresolved)"
    else:
        kb_value = f" ({resolved_context.kb_root.as_posix()})"
    lines.append(
        "- Knowledge base (KB; durable project knowledge): "
        f"`$MERIDIAN_CONTEXT_KB_DIR`{kb_value}; locate it with `meridian context kb`."
    )

    other_context: list[str] = []
    if resolved_context.work_archive is not None:
        other_context.append(
            "work archive "
            f"`${context_env_key('work_archive')}` "
            f"({resolved_context.work_archive.as_posix()})"
        )
    for name in sorted(resolved_context.extra):
        path, _ = resolved_context.extra[name]
        other_context.append(
            f"{name} `${context_env_key(name)}` ({path.as_posix()})"
        )
    if other_context:
        lines.append(
            "- Other resolved context directories: "
            + "; ".join(other_context)
            + "; inspect with `meridian context`."
        )

    return "\n".join(lines)


__all__ = [
    "build_context_prompt",
    "compose_skill_prompt_documents",
]
