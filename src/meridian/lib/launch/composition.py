from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from meridian.lib.launch.reference import ReferenceItem

from meridian.lib.core.domain import AvailableSkillEntry
from meridian.lib.launch.reference import (
    measure_rendered_reference_block_bytes,
    render_reference_blocks,
)

SYSTEM_INSTRUCTION_BLOCK_ORDER: tuple[str, ...] = (
    "launch_preamble",
    "agent_profile_body",
    "supplemental_documents",
    "available_skills_listing",
    "guidance_blocks",
    "report_instruction",
)

INLINE_BLOCK_ORDER: tuple[str, ...] = (
    "system_instruction",
    "task_context",
    "user_task_prompt",
)


class GuidancePhase(IntEnum):
    GUIDANCE = 0
    ENVIRONMENT = 1


CAPABILITIES_BLOCK_NAME = "capabilities"


@dataclass(frozen=True)
class CompositionBlock:
    name: str
    phase: GuidancePhase
    priority: int = 0
    content: str = ""


@dataclass(frozen=True)
class PromptDocument:
    kind: Literal["skill", "bootstrap"]
    logical_name: str
    path: str
    content: str
    skill_type: str = "reference"


@dataclass(frozen=True)
class ReferenceRouting:
    path: str
    type: Literal["file", "directory"]
    routing: Literal["inline", "native-injection", "omitted"]
    native_flag: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "type": self.type,
            "routing": self.routing,
            "native_flag": self.native_flag,
        }


@dataclass(frozen=True)
class InlineFileReferenceContribution:
    path: str
    byte_count: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True)
class ProjectionChannels:
    system_instruction: Literal[
        "append-system-prompt", "system-field", "config-agent", "inline", "none"
    ]
    user_task_prompt: Literal["user-turn", "inline"]
    task_context: Literal["user-turn", "inline", "native-injection"]

    def to_dict(self) -> dict[str, str]:
        return {
            "system_instruction": self.system_instruction,
            "user_task_prompt": self.user_task_prompt,
            "task_context": self.task_context,
        }


@dataclass(frozen=True)
class ComposedLaunchContent:
    supplemental_documents: tuple[PromptDocument, ...]
    """Typed supplemental docs; rendered skill docs before bootstrap docs."""

    agent_profile_body: str
    """Agent body (when not delivered via native agents)."""

    report_instruction: str
    """The 'write a report' directive."""

    passthrough_system_fragments: tuple[str, ...]
    """Explicit --append-system-prompt passthrough args; appended last."""

    user_task_prompt: str
    """Raw user request, template-substituted."""

    reference_items: tuple[ReferenceItem, ...]
    """Structured reference files/dirs for adapter-owned routing and rendering."""

    prior_output: str
    """Sanitized prior-run output."""

    launch_preamble: str = ""
    """Launch-mode-specific behavioral framing."""

    available_skills: tuple[AvailableSkillEntry, ...] = ()
    """On-demand skills listed by name in the prompt (not loaded until triggered)."""

    guidance_blocks: tuple[CompositionBlock, ...] = ()
    """Ordered guidance (skill triggers, capabilities, prompting, rules, context paths)."""


def composed_content_for_bind_refresh(
    composed: ComposedLaunchContent,
) -> ComposedLaunchContent | None:

    if not has_capabilities_block(composed.guidance_blocks):
        return None
    return composed


@dataclass(frozen=True)
class ProjectedContent:
    system_prompt: str
    """Goes to --append-system-prompt channel (empty = omit)."""

    user_turn_content: str
    """Goes to user-turn / inline prompt channel."""

    reference_routing: tuple[ReferenceRouting, ...]
    """Per-reference routing decisions."""

    channels: ProjectionChannels
    """Per-category routing decisions for projection-manifest.json."""


def build_reference_routing(
    reference_items: tuple[ReferenceItem, ...],
) -> tuple[ReferenceRouting, ...]:
    return tuple(
        ReferenceRouting(
            path=item.path.as_posix(),
            type=item.kind,
            routing=(
                "omitted"
                if item.kind == "file" and not item.body.strip() and not item.warning
                else "inline"
            ),
            native_flag=None,
        )
        for item in reference_items
    )


def build_inline_file_contributions(
    reference_items: tuple[ReferenceItem, ...],
    reference_routing: tuple[ReferenceRouting, ...],
    *,
    # Warning-only references must not trigger context-drain warnings.
    exclude_warning_only: bool = False,
) -> tuple[InlineFileReferenceContribution, ...]:

    contributions = tuple(
        InlineFileReferenceContribution(
            path=item.path.as_posix(),
            byte_count=measure_rendered_reference_block_bytes(
                item, include_warning_only=not exclude_warning_only
            ),
        )
        for item, route in zip(reference_items, reference_routing, strict=True)
        if item.kind == "file"
        and route.routing == "inline"
        and measure_rendered_reference_block_bytes(
            item, include_warning_only=not exclude_warning_only
        )
        > 0
    )
    return tuple(
        sorted(
            contributions,
            key=lambda contribution: (-contribution.byte_count, contribution.path),
        )
    )


def join_content_blocks(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block.strip())


def has_capabilities_block(guidance_blocks: tuple[CompositionBlock, ...]) -> bool:

    return any(block.name == CAPABILITIES_BLOCK_NAME for block in guidance_blocks)


def replace_capabilities_block(
    guidance_blocks: tuple[CompositionBlock, ...],
    *,
    new_content: str,
) -> tuple[CompositionBlock, ...]:

    if not has_capabilities_block(guidance_blocks):
        return guidance_blocks
    normalized = new_content.strip()
    return tuple(
        replace(block, content=normalized) if block.name == CAPABILITIES_BLOCK_NAME else block
        for block in guidance_blocks
    )


_SKILL_TYPE_DESCRIPTIONS: dict[str, str] = {
    "principle": "Override other guidance when loaded.",
    "guardrail": "Load before acting in sensitive areas.",
    "mode-shift": "Change how you operate when loaded.",
    "checkpoint": "Load at decision points to verify before continuing.",
}


def _loaded_skill_type_header(skill_type: str) -> str:
    label = skill_type.capitalize()
    desc = _SKILL_TYPE_DESCRIPTIONS.get(skill_type)
    if desc:
        return f"## {label}\n{desc}"
    return f"## {label}"


def _render_available_skills_block(available_skills: tuple[AvailableSkillEntry, ...]) -> str:
    if not available_skills:
        return ""

    lines = [
        "# Available Skills",
        "",
        "Not yet loaded. Load proactively when the task fits.",
    ]
    known_type_order = ("principle", "guardrail", "mode-shift", "checkpoint")

    seen_types: list[str] = []
    for t in known_type_order:
        if any(s.skill_type == t for s in available_skills):
            seen_types.append(t)
    for s in available_skills:
        if s.skill_type not in seen_types:
            seen_types.append(s.skill_type)

    for type_key in seen_types:
        group = [s for s in available_skills if s.skill_type == type_key]
        if not group:
            continue
        label = type_key.capitalize()
        desc = _SKILL_TYPE_DESCRIPTIONS.get(type_key)
        if desc:
            lines.append(f"\n## {label}\n{desc}")
        else:
            lines.append(f"\n## {label}")
        for skill in group:
            suffix = f": {skill.description}" if skill.description else ""
            lines.append(f"- {skill.name}{suffix}")

    return "\n".join(lines)


def render_system_instruction_blocks(content: ComposedLaunchContent) -> str:
    skill_type_priority = {"principle": 0, "guardrail": 1, "reference": 2}
    skill_documents = tuple(
        document for document in content.supplemental_documents if document.kind == "skill"
    )
    ordered_skill_documents = tuple(
        sorted(
            skill_documents,
            key=lambda document: skill_type_priority.get(document.skill_type, 2),
        )
    )
    bootstrap_documents = tuple(
        document for document in content.supplemental_documents if document.kind == "bootstrap"
    )

    ordered_blocks: list[str] = []
    for field_name in SYSTEM_INSTRUCTION_BLOCK_ORDER:
        if field_name == "supplemental_documents":
            if ordered_skill_documents:
                current_type: str | None = None
                for doc in ordered_skill_documents:
                    if doc.content.strip():
                        if doc.skill_type != current_type:
                            current_type = doc.skill_type
                            header = _loaded_skill_type_header(current_type)
                            if header:
                                ordered_blocks.append(header)
                        ordered_blocks.append(doc.content.strip())
            ordered_blocks.extend(document.content for document in bootstrap_documents)
            continue
        if field_name == "available_skills_listing":
            listing = _render_available_skills_block(content.available_skills)
            if listing:
                ordered_blocks.append(listing)
            continue
        if field_name == "guidance_blocks":
            ordered = sorted(content.guidance_blocks, key=lambda b: (b.phase, b.priority))
            for block in ordered:
                if block.content.strip():
                    ordered_blocks.append(block.content)
            continue
        ordered_blocks.append(getattr(content, field_name))
    return join_content_blocks(*ordered_blocks, *content.passthrough_system_fragments)


def render_task_context(
    reference_items: tuple[ReferenceItem, ...],
    reference_routing: tuple[ReferenceRouting, ...],
    prior_output: str,
) -> str:
    inline_references = tuple(
        item
        for item, route in zip(
            reference_items,
            reference_routing,
            strict=True,
        )
        if route.routing == "inline"
    )
    reference_blocks = render_reference_blocks(inline_references)
    return join_content_blocks(*reference_blocks, prior_output)


def project_inline_content(content: ComposedLaunchContent) -> ProjectedContent:
    reference_routing = build_reference_routing(content.reference_items)
    system_text = render_system_instruction_blocks(content)
    task_context = render_task_context(
        content.reference_items,
        reference_routing,
        content.prior_output,
    )
    inline_blocks = {
        "system_instruction": system_text,
        "task_context": task_context,
        "user_task_prompt": content.user_task_prompt,
    }
    user_turn = join_content_blocks(*(inline_blocks[name] for name in INLINE_BLOCK_ORDER))

    return ProjectedContent(
        system_prompt="",
        user_turn_content=user_turn,
        reference_routing=reference_routing,
        channels=ProjectionChannels(
            system_instruction="inline",
            user_task_prompt="inline",
            task_context="inline",
        ),
    )


__all__ = [
    "CAPABILITIES_BLOCK_NAME",
    "INLINE_BLOCK_ORDER",
    "SYSTEM_INSTRUCTION_BLOCK_ORDER",
    "ComposedLaunchContent",
    "CompositionBlock",
    "GuidancePhase",
    "InlineFileReferenceContribution",
    "ProjectedContent",
    "ProjectionChannels",
    "PromptDocument",
    "ReferenceRouting",
    "build_inline_file_contributions",
    "build_reference_routing",
    "composed_content_for_bind_refresh",
    "has_capabilities_block",
    "join_content_blocks",
    "project_inline_content",
    "render_system_instruction_blocks",
    "render_task_context",
    "replace_capabilities_block",
]
