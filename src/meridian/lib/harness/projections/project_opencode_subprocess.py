"""OpenCode subprocess command projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections._prompt_arg import check_prompt_argument
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
        "permission_policy",
        "extra_args",
        "interactive",
        "mcp_tools",
        "projected_roots",
        "agent_name",
        "skills",
        "opencode_version",
    }
)

# ``appended_system_prompt`` is delivered at launch bind as a config agent
# (``project_opencode_config.py``), not by this projection.
_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "native_identity",
        "appended_system_prompt",
        "agents_payload",
        "claude_runtime",
        "base_instructions",
        "developer_instructions",
        "harness",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
        "prompt_file_path",
        "reference_items",
        "report_output_path",
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

    harness_session_id = (spec.continue_session_id or "").strip()
    resume_with_model = bool(harness_session_id) and spec.model is not None and not spec.interactive
    if resume_with_model and spec.opencode_version == "v1":
        # ``opencode run --session`` cannot switch the model committed to a
        # resumed session on V1. V2 supports it (``POST /api/session/{id}/model``)
        # and keeps the flag, so the guard must be version-aware. Fail loudly
        # only for a known V1 rather than silently forwarding a flag the V1
        # transport does not honor. Interactive primary launches use managed
        # attach and the version-aware streaming guard.
        raise HarnessCapabilityMismatch(
            "OpenCode subprocess cannot switch the model when resuming a session "
            f"(requested model '{spec.model}'). Resume without an explicit model, "
            "or use OpenCode 2's streaming transport, which applies it via "
            "POST /api/session/{id}/model."
        )

    if spec.skills:
        logger.debug(
            "OpenCode subprocess received spec.skills but has no native skills flag; "
            "skills must be delivered by prompt injection or streaming payload"
        )
    if spec.projected_roots:
        logger.debug(
            "OpenCode subprocess workspace roots are projected via %s env.",
            "OPENCODE_CONFIG_CONTENT",
        )

    command: list[str] = list(base_command)

    # V2's bare TUI rejects ``--model`` and ``--agent`` (a fresh session's model and
    # default agent ride the bound config) and otherwise attaches to the shared
    # background service, which never sees this launch's env (config content,
    # OPENCODE_DB). A resumed V2 session therefore keeps its stored agent here.
    v2_tui = spec.interactive and spec.opencode_version == "v2"
    if v2_tui:
        command.append("--standalone")

    model_arg = None if v2_tui else spec.model
    if model_arg is not None:
        command.extend(("--model", model_arg))

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

    agent_arg = None if v2_tui else spec.agent_name
    if agent_arg is not None:
        command.extend(("--agent", agent_arg))

    has_continue_session = bool(harness_session_id)
    has_continue_fork = has_continue_session and spec.continue_fork
    passthrough_tail = spec.extra_args

    _log_collision_if_needed(
        managed_flag="--model",
        has_managed_value=model_arg is not None,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--variant",
        has_managed_value=bool(normalized_effort) and not spec.interactive,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--agent",
        has_managed_value=agent_arg is not None,
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

    command.extend(passthrough_tail)

    if not spec.interactive:
        command.append("-")

    if has_continue_session:
        command.extend(("--session", harness_session_id))
        if has_continue_fork:
            command.append("--fork")

    if spec.interactive and spec.prompt:
        # This is the bare-TUI fallback when managed attach is unavailable.
        command.extend(("--prompt", check_prompt_argument(spec.prompt)))

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
