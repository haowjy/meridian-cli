"""Cursor subprocess command projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)

_PROJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "prompt",
        "permission_resolver",
        "extra_args",
        "task_cwd",
        "effort",
        "candidate_slugs",
    }
)

_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "harness",
        "continue_session_id",
        "continue_fork",
        "interactive",
        "mcp_tools",
        "projected_roots",
        "agent_name",
        "appended_system_prompt",
        "agents_payload",
        "prompt_file_path",
        "user_turn_content",
        "report_output_path",
        "base_instructions",
        "developer_instructions",
        "skills",
        "reference_items",
        "pi_extension_entrypoints",
    }
)


def _project_approval_flags(spec: ResolvedLaunchSpec) -> tuple[str, ...]:
    approval_mode = spec.permission_resolver.config.approval
    if approval_mode == "yolo":
        return ("--yolo",)
    if approval_mode == "auto":
        return ("--force",)
    if approval_mode in {"default", "confirm"}:
        return ()
    raise HarnessCapabilityMismatch(
        f"Cursor projection does not support approval mode '{approval_mode}'."
    )


def _assert_supported_for_mvp(spec: ResolvedLaunchSpec) -> None:
    if spec.mcp_tools:
        raise HarnessCapabilityMismatch(
            "Cursor subprocess does not support per-spawn mcp_tools for MVP."
        )

    if spec.continue_fork:
        raise HarnessCapabilityMismatch(
            "Cursor subprocess continue_fork is not supported for MVP."
        )

    if (spec.continue_session_id or "").strip():
        raise HarnessCapabilityMismatch(
            "Cursor subprocess session resume is not supported for MVP."
        )

    if spec.interactive:
        raise HarnessCapabilityMismatch(
            "Cursor subprocess interactive mode is not supported for MVP."
        )


def _prefix_matches(slug: str, model: str) -> bool:
    """True when *slug* is *model* itself or starts with ``model-``."""
    return slug == model or slug.startswith(f"{model}-")


def _resolve_cursor_model(
    model: str,
    effort: str | None,
    candidate_slugs: Sequence[str],
) -> str:
    """Resolve the best cursor slug for model + effort.

    Rules:
    1. Exact match in catalog with no effort → pass through verbatim.
    2. No effort → return model unchanged.
    3. Search candidates starting with ``model-`` containing ``-effort``
       as a complete trailing segment; prefer thinking variants (D5).
    4. No match → fallback to ``{model}-{effort}``.
    """

    normalized_effort = effort.strip().lower() if effort and effort.strip() else None

    # Rule 1: exact catalog match with no effort override → verbatim.
    if model in candidate_slugs and not normalized_effort:
        return model

    if not normalized_effort:
        return model

    # Rule 3: search candidates for boundary-aware prefix + effort match.
    # Effort must appear as a trailing segment (endswith) or before a known
    # suffix like -thinking. When multiple slugs match (e.g. both "gpt-5.5-high"
    # and "gpt-5.5-extra-high" end with "-high"), the shortest match wins.
    effort_suffix = f"-{normalized_effort}"
    effort_matches = [
        slug
        for slug in candidate_slugs
        if _prefix_matches(slug, model)
        and (slug.endswith(effort_suffix) or f"{effort_suffix}-" in slug)
    ]

    if len(effort_matches) == 1:
        return effort_matches[0]

    if len(effort_matches) > 1:
        # D5: prefer thinking variants (cursor Claude naming convention).
        thinking = [slug for slug in effort_matches if "thinking" in slug]
        if thinking:
            return min(thinking, key=len)
        return min(effort_matches, key=len)

    return f"{model}-{normalized_effort}"


def project_cursor_spec_to_cli_args(
    spec: ResolvedLaunchSpec,
    *,
    base_command: tuple[str, ...],
) -> list[str]:
    """Project one ``ResolvedLaunchSpec`` into an ordered subprocess command list."""

    _assert_supported_for_mvp(spec)
    if spec.projected_roots:
        logger.debug(
            "Cursor subprocess received projected_roots but MVP only projects --workspace "
            "from task_cwd; ignoring extra workspace roots."
        )

    command: list[str] = list(base_command)

    if spec.model is not None:
        resolved_model = _resolve_cursor_model(spec.model, spec.effort, spec.candidate_slugs)
        command.extend(("--model", resolved_model))

    command.extend(_project_approval_flags(spec))

    workspace = (spec.task_cwd or "").strip()
    if workspace:
        command.extend(("--workspace", workspace))

    if spec.extra_args:
        command.extend(spec.extra_args)

    command.append(spec.prompt)
    return command


_check_projection_drift(
    ResolvedLaunchSpec,
    projected=_PROJECTED_FIELDS,
    delegated=_DELEGATED_FIELDS,
)


__all__ = [
    "HarnessCapabilityMismatch",
    "project_cursor_spec_to_cli_args",
]
