"""Semantic content models for launch composition.

This module provides harness-agnostic data structures for classified
launch content. The models follow the Semantic IR + Adapter Projection
pattern: composition code classifies content by meaning, then harness
adapters decide how to route content to CLI channels.

See spec S-1 for category definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from meridian.lib.launch.reference import ReferenceItem

from meridian.lib.launch.reference import render_reference_blocks

# Named canonical orders for SYSTEM_INSTRUCTION composition.
SYSTEM_INSTRUCTION_BLOCK_ORDER: tuple[str, ...] = (
    "supplemental_documents",
    "agent_profile_body",
    "report_instruction",
    "inventory_prompt",
    "context_prompt",
    "completion_contract",
    # passthrough_system_fragments appended last
)

# Canonical ordering for inline harnesses (Codex, OpenCode).
INLINE_BLOCK_ORDER: tuple[str, ...] = (
    "system_instruction",
    "task_context",
    "user_task_prompt",
)


@dataclass(frozen=True)
class PromptDocument:
    """Typed supplemental document injected into launch prompts."""

    kind: Literal["skill", "bootstrap"]
    logical_name: str
    path: str
    content: str


@dataclass(frozen=True)
class ReferenceRouting:
    """Per-reference routing decision after harness projection.

    Captures how a single reference item was routed by the harness adapter.
    Serializes to the S-4c JSON schema.
    """

    path: str
    type: Literal["file", "directory"]
    routing: Literal["inline", "native-injection", "omitted"]
    native_flag: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Serialize to S-4c schema dict."""
        return {
            "path": self.path,
            "type": self.type,
            "routing": self.routing,
            "native_flag": self.native_flag,
        }


@dataclass(frozen=True)
class ProjectionChannels:
    """Adapter-resolved channel decisions for semantic content categories."""

    system_instruction: Literal["append-system-prompt", "system-field", "inline", "none"]
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
    """Semantic content blocks before harness channel projection.

    Fields are semantic blocks plus structured references.
    Harness adapters decide how to combine and route them.

    There are three semantic categories (see spec S-1):
      SYSTEM_INSTRUCTION — controls agent behavior
      USER_TASK_PROMPT   — user-supplied request text
      TASK_CONTEXT       — reference files, dirs, prior-run output

    Template variable expansions are not a category. Substitution
    happens in-place; expanded text inherits the category of the
    containing block.
    """

    # SYSTEM_INSTRUCTION blocks
    supplemental_documents: tuple[PromptDocument, ...]
    """Typed supplemental docs; rendered skill docs before bootstrap docs."""

    agent_profile_body: str
    """Agent body (when not delivered via native agents)."""

    report_instruction: str
    """The 'write a report' directive."""

    inventory_prompt: str
    """Agent inventory — SYSTEM_INSTRUCTION, not startup context."""

    context_prompt: str
    """Resolved context directories with env var names."""

    passthrough_system_fragments: tuple[str, ...]
    """Explicit --append-system-prompt passthrough args; appended last."""

    # USER_TASK_PROMPT
    user_task_prompt: str
    """Raw user request, template-substituted."""

    # TASK_CONTEXT blocks
    reference_items: tuple[ReferenceItem, ...]
    """Structured reference files/dirs for adapter-owned routing and rendering."""

    prior_output: str
    """Sanitized prior-run output."""

    completion_contract: str = ""
    """Deterministic bounded completion contract text (spawn goal)."""


@dataclass(frozen=True)
class ProjectedContent:
    """Output of harness content projection.

    Represents the harness adapter's decision about how to route
    semantic content blocks to CLI channels.
    """

    system_prompt: str
    """Goes to --append-system-prompt channel (empty = omit)."""

    user_turn_content: str
    """Goes to user-turn / inline prompt channel."""

    reference_routing: tuple[ReferenceRouting, ...]
    """Per-reference routing decisions."""

    channels: ProjectionChannels
    """Per-category routing decisions for projection-manifest.json."""

    def channel_manifest(self) -> dict[str, str]:
        """Generate channel routing for projection-manifest.json (S-4d)."""
        return self.channels.to_dict()


def build_reference_routing(
    reference_items: tuple[ReferenceItem, ...],
) -> tuple[ReferenceRouting, ...]:
    """Build reference routing decisions from items.

    Files with empty body and no warning are omitted.
    All other items route inline (no native injection).
    """
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


def join_content_blocks(*blocks: str) -> str:
    """Join non-empty content blocks with double newlines."""
    return "\n\n".join(block.strip() for block in blocks if block.strip())


def render_system_instruction_blocks(content: ComposedLaunchContent) -> str:
    """Render SYSTEM_INSTRUCTION blocks in canonical order.

    Order: skill supplemental documents, bootstrap supplemental documents,
    agent_profile_body, report_instruction, inventory_prompt, context_prompt,
    completion_contract, then passthrough_system_fragments last.
    """
    ordered_blocks: list[str] = []
    for field_name in SYSTEM_INSTRUCTION_BLOCK_ORDER:
        if field_name == "supplemental_documents":
            ordered_blocks.extend(
                document.content
                for kind in ("skill", "bootstrap")
                for document in content.supplemental_documents
                if document.kind == kind
            )
            continue
        ordered_blocks.append(getattr(content, field_name))
    return join_content_blocks(*ordered_blocks, *content.passthrough_system_fragments)


def render_task_context(
    reference_items: tuple[ReferenceItem, ...],
    reference_routing: tuple[ReferenceRouting, ...],
    prior_output: str,
) -> str:
    """Render TASK_CONTEXT: inline references + prior output."""
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
    """Project all content inline with canonical INLINE_BLOCK_ORDER.

    Ordering: SYSTEM_INSTRUCTION -> TASK_CONTEXT -> USER_TASK_PROMPT
    Used by Codex, OpenCode, and the base adapter default.
    """
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
    "INLINE_BLOCK_ORDER",
    "SYSTEM_INSTRUCTION_BLOCK_ORDER",
    "ComposedLaunchContent",
    "ProjectedContent",
    "ProjectionChannels",
    "PromptDocument",
    "ReferenceRouting",
    "build_reference_routing",
    "join_content_blocks",
    "project_inline_content",
    "render_system_instruction_blocks",
    "render_task_context",
]
