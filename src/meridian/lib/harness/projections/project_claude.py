"""Claude command-line projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.claude_preflight import CLAUDE_PARENT_ALLOWED_TOOLS_FLAG
from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.permission_flags import resolve_permission_flags
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.text_utils import dedupe_nonempty, split_csv_entries

logger = logging.getLogger(__name__)

_PROJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "agent_name",
        "agents_payload",
        "appended_system_prompt",
        "continue_fork",
        "continue_session_id",
        "effort",
        "extra_args",
        "interactive",
        "mcp_tools",
        "projected_roots",
        "model",
        "permission_resolver",
        "prompt",
        "prompt_file_path",
        "user_turn_content",
    }
)

_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "base_instructions",
        "developer_instructions",
        "harness",
        "pi_extension_entrypoints",
        "reference_items",
        "report_output_path",
        "skills",
    }
)


def _split_internal_parent_allowed_tools(
    extra_args: tuple[str, ...],
) -> tuple[tuple[str, ...], list[str]]:
    passthrough_tail: list[str] = []
    parent_allowed_tools: list[str] = []

    index = 0
    while index < len(extra_args):
        token = extra_args[index]
        if token == CLAUDE_PARENT_ALLOWED_TOOLS_FLAG:
            if index + 1 < len(extra_args):
                parent_allowed_tools.extend(split_csv_entries(extra_args[index + 1]))
                index += 2
                continue
            index += 1
            continue
        if token.startswith(f"{CLAUDE_PARENT_ALLOWED_TOOLS_FLAG}="):
            parent_allowed_tools.extend(split_csv_entries(token.partition("=")[2]))
            index += 1
            continue
        passthrough_tail.append(token)
        index += 1

    return tuple(passthrough_tail), dedupe_nonempty(parent_allowed_tools)


def _extract_claude_tool_flags(
    permission_flags: tuple[str, ...],
) -> tuple[list[str], list[str], list[str]]:
    projected_permission_flags: list[str] = []
    allowed_tools: list[str] = []
    disallowed_tools: list[str] = []

    index = 0
    while index < len(permission_flags):
        token = permission_flags[index]

        if token == "--allowedTools":
            if index + 1 < len(permission_flags):
                allowed_tools.extend(split_csv_entries(permission_flags[index + 1]))
                index += 2
                continue
            index += 1
            continue

        if token.startswith("--allowedTools="):
            allowed_tools.extend(split_csv_entries(token.partition("=")[2]))
            index += 1
            continue

        if token == "--disallowedTools":
            if index + 1 < len(permission_flags):
                disallowed_tools.extend(split_csv_entries(permission_flags[index + 1]))
                index += 2
                continue
            index += 1
            continue

        if token.startswith("--disallowedTools="):
            disallowed_tools.extend(split_csv_entries(token.partition("=")[2]))
            index += 1
            continue

        projected_permission_flags.append(token)
        index += 1

    return (
        projected_permission_flags,
        dedupe_nonempty(allowed_tools),
        dedupe_nonempty(disallowed_tools),
    )


def _has_flag(args: Sequence[str], flag: str) -> bool:
    return any(token == flag or token.startswith(f"{flag}=") for token in args)


def _log_collision_if_needed(
    *,
    managed_flag: str,
    has_managed_value: bool,
    passthrough_tail: tuple[str, ...],
) -> None:
    if not has_managed_value or not _has_flag(passthrough_tail, managed_flag):
        return

    message = (
        "Claude projection known managed flag %s also present in extra_args; "
        "user tail value wins by last-wins semantics"
    )
    logger.debug(message, managed_flag)


def _project_mcp_tools(mcp_tools: Iterable[str]) -> list[str]:
    projected: list[str] = []
    for tool in mcp_tools:
        normalized = tool.strip()
        if not normalized:
            continue
        projected.extend(("--mcp-config", normalized))
    return projected


def project_claude_spec_to_cli_args(
    spec: ResolvedLaunchSpec,
    *,
    base_command: tuple[str, ...],
) -> list[str]:
    """Project one ``ResolvedLaunchSpec`` into an ordered command list."""

    command: list[str] = list(base_command)
    if not spec.interactive:
        command.append("-")

    if spec.model:
        command.extend(("--model", spec.model))
    if spec.effort:
        command.extend(("--effort", spec.effort))
    if spec.agent_name:
        command.extend(("--agent", str(spec.agent_name)))

    passthrough_tail, parent_allowed_tools = _split_internal_parent_allowed_tools(spec.extra_args)

    permission_flags = resolve_permission_flags(spec.permission_resolver, HarnessId.CLAUDE)
    permission_tail, allowed_tools, disallowed_tools = _extract_claude_tool_flags(permission_flags)
    allowed_tools = dedupe_nonempty((*allowed_tools, *parent_allowed_tools))
    command.extend(permission_tail)
    if allowed_tools:
        command.extend(("--allowedTools", ",".join(allowed_tools)))
    if disallowed_tools:
        command.extend(("--disallowedTools", ",".join(disallowed_tools)))

    command.extend(_project_mcp_tools(spec.mcp_tools))

    if spec.appended_system_prompt:
        if spec.prompt_file_path:
            # Use file-based delivery to avoid ARG_MAX limits
            command.extend(("--append-system-prompt-file", spec.prompt_file_path))
        else:
            # Fallback to inline delivery
            command.extend(("--append-system-prompt", spec.appended_system_prompt))
    if spec.agents_payload:
        command.extend(("--agents", spec.agents_payload))

    harness_session_id = (spec.continue_session_id or "").strip()
    if harness_session_id:
        command.extend(("--resume", harness_session_id))
        if spec.continue_fork:
            command.append("--fork-session")

    for root in spec.projected_roots:
        command.extend(("--add-dir", root.as_posix()))

    _log_collision_if_needed(
        managed_flag="--allowedTools",
        has_managed_value=bool(allowed_tools),
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--disallowedTools",
        has_managed_value=bool(disallowed_tools),
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--append-system-prompt",
        has_managed_value=bool(spec.appended_system_prompt),
        passthrough_tail=passthrough_tail,
    )

    command.extend(passthrough_tail)

    # For interactive Claude launches, add user-turn content as positional argument
    # This routes USER_TASK_PROMPT and TASK_CONTEXT to the user-turn channel
    # instead of --append-system-prompt (spec S-2a)
    if spec.interactive and spec.user_turn_content:
        command.append(spec.user_turn_content)

    return command


_check_projection_drift(
    ResolvedLaunchSpec,
    projected=_PROJECTED_FIELDS,
    delegated=_DELEGATED_FIELDS,
)


__all__ = [
    "_DELEGATED_FIELDS",
    "_PROJECTED_FIELDS",
    "_check_projection_drift",
    "project_claude_spec_to_cli_args",
]
