"""OpenCode subprocess command projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.permission_flags import resolve_permission_flags
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)


_PROJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "effort",
        "prompt",
        "continue_session_id",
        "continue_fork",
        "permission_resolver",
        "extra_args",
        "interactive",
        "mcp_tools",
        "projected_roots",
        "agent_name",
        "appended_system_prompt",
        "skills",
    }
)

_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "agents_payload",
        "claude_native_agents_enabled",
        "base_instructions",
        "developer_instructions",
        "harness",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
        "disallowed_tools",
        "prompt_file_path",
        "reference_items",
        "report_output_path",
        "web_search_enabled",
        "user_turn_content",
        "task_cwd",
    }
)


def _normalized_nonempty(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        token = value.strip()
        if token:
            normalized.append(token)
    return tuple(normalized)


_MANAGED_FLAG_ALIASES: dict[str, tuple[str, ...]] = {
    "--model": ("--model", "-m"),
    "--variant": ("--variant",),
    "--agent": ("--agent",),
    "--session": ("--session", "-s", "--continue", "-c"),
    "--fork": ("--fork",),
}


def _has_flag(args: Sequence[str], flag: str) -> bool:
    return any(token == flag or token.startswith(f"{flag}=") for token in args)


def _log_collision_if_needed(
    *,
    managed_flag: str,
    has_managed_value: bool,
    passthrough_tail: tuple[str, ...],
) -> None:
    if not has_managed_value:
        return

    aliases = _MANAGED_FLAG_ALIASES.get(managed_flag, (managed_flag,))
    if not any(_has_flag(passthrough_tail, alias) for alias in aliases):
        return

    logger.debug(
        "OpenCode projection known managed flag %s also present in extra_args; "
        "user tail value wins by last-wins semantics",
        managed_flag,
    )


def project_opencode_spec_to_cli_args(
    spec: ResolvedLaunchSpec,
    *,
    base_command: tuple[str, ...],
) -> list[str]:
    """Project one ``ResolvedLaunchSpec`` into an ordered subprocess command list."""

    projected_mcp_tools = _normalized_nonempty(spec.mcp_tools)
    if projected_mcp_tools:
        raise HarnessCapabilityMismatch(
            "OpenCode subprocess does not support per-spawn mcp_tools; "
            "use streaming transport (opencode serve) for MCP session payloads."
        )

    if spec.skills:
        logger.debug(
            "OpenCode subprocess received spec.skills but has no native skills flag; "
            "skills must be delivered by prompt injection or streaming payload"
        )
    if spec.appended_system_prompt:
        logger.debug(
            "OpenCode subprocess cannot project appended_system_prompt separately; "
            "streaming transport sends it via the message system field"
        )
    if spec.projected_roots:
        logger.debug(
            "OpenCode subprocess workspace roots are projected via %s env.",
            "OPENCODE_CONFIG_CONTENT",
        )

    command: list[str] = list(base_command)

    if spec.model is not None:
        command.extend(("--model", spec.model))

    normalized_effort = (spec.effort or "").strip()
    if normalized_effort:
        if spec.interactive:
            # The bare ``opencode`` TUI command does not accept ``--variant``;
            # only ``opencode run`` does.  Silently drop it for interactive launches.
            logger.debug(
                "OpenCode TUI does not support --variant; ignoring effort=%s",
                normalized_effort,
            )
        else:
            command.extend(("--variant", normalized_effort))

    if spec.agent_name:
        command.extend(("--agent", spec.agent_name))

    harness_session_id = (spec.continue_session_id or "").strip()
    has_continue_session = bool(harness_session_id)
    has_continue_fork = has_continue_session and spec.continue_fork
    passthrough_tail = spec.extra_args

    _log_collision_if_needed(
        managed_flag="--model",
        has_managed_value=spec.model is not None,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--variant",
        has_managed_value=bool(normalized_effort) and not spec.interactive,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--agent",
        has_managed_value=bool(spec.agent_name),
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--session",
        has_managed_value=has_continue_session,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--fork",
        has_managed_value=has_continue_fork,
        passthrough_tail=passthrough_tail,
    )

    command.extend(resolve_permission_flags(spec.permission_resolver, HarnessId.OPENCODE))

    command.extend(passthrough_tail)

    if spec.interactive:
        # Interactive TUI launches go through managed attach (opencode serve →
        # HTTP API → opencode attach).  The system prompt is delivered via the
        # message system field, and the user types the first message.  Passing
        # --prompt here would inject boilerplate as a user turn.
        pass
    else:
        command.append("-")

    if has_continue_session:
        command.extend(("--session", harness_session_id))
        if has_continue_fork:
            command.append("--fork")

    return command


_check_projection_drift(
    ResolvedLaunchSpec,
    projected=_PROJECTED_FIELDS,
    delegated=_DELEGATED_FIELDS,
)


__all__ = [
    "_DELEGATED_FIELDS",
    "_PROJECTED_FIELDS",
    "HarnessCapabilityMismatch",
    "_check_projection_drift",
    "project_opencode_spec_to_cli_args",
]
