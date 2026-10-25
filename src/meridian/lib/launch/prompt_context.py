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
    spawn_id: str | None = None,
    chat_id: str | None = None,
    work_id: str | None = None,
    context_config: ContextConfig | None = None,
    resolved_context: ResolvedContextPaths | None = None,
) -> str:
    """Render late-bound directory capability lines for launch guidance.

    Each source of launch state appears once: resolved value first, its
    environment-variable label, and the command that inspects it.
    """

    if resolved_context is None:
        if context_config is None:
            from meridian.lib.state.paths import load_context_config

            context_config = load_context_config(project_root) or ContextConfig()
        resolved_context = resolve_context_paths(project_root, context_config)

    lines: list[str] = []
    if active_work_dir is None:
        lines.append(
            "- No active work directory (`$MERIDIAN_ACTIVE_WORK_DIR`); inspect with "
            "`meridian work current`."
        )
    else:
        work_item = f" for work item `{work_id}`" if (work_id or "").strip() else ""
        lines.append(
            f"- `{active_work_dir.as_posix()}` — work directory for scratch/artifacts "
            f"(`$MERIDIAN_ACTIVE_WORK_DIR`{work_item}); `meridian work current`."
        )
    lines.append(
        f"- `{task_dir.as_posix()}` — source checkout for reads/edits/git/builds/tests "
        "(`$MERIDIAN_TASK_DIR`); shell cwd stays at "
        f"`{project_root.as_posix()}` (`$MERIDIAN_PROJECT_DIR`), so use absolute paths; "
        "inspect: `meridian task-dir`; set work item: `meridian work task-dir`; one spawn: "
        "`meridian spawn --task-dir`."
    )

    if resolved_context.kb_root is None:
        lines.append(
            "- No resolved knowledge base (`$MERIDIAN_CONTEXT_KB_DIR`); inspect with "
            "`meridian context kb`."
        )
    else:
        lines.append(
            f"- `{resolved_context.kb_root.as_posix()}` — durable knowledge base "
            "(`$MERIDIAN_CONTEXT_KB_DIR`); `meridian context kb`."
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
            f"- `{current_chat_id}` — current chat ID (`$MERIDIAN_CHAT_ID`), parent for "
            "handoff `--from`."
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
        lines.append(
            "- "
            + "; ".join(other_context)
            + "; `meridian context`."
        )

    return "\n".join(lines)


__all__ = [
    "build_context_prompt",
    "compose_skill_prompt_documents",
]
