"""Pi RPC command projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.permission_flags import resolve_permission_flags
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)

_PROJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "effort",
        "continue_session_id",
        "continue_fork",
        "permission_resolver",
        "extra_args",
        "interactive",
        "mcp_tools",
        "projected_roots",
        "appended_system_prompt",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
    }
)

_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "harness",
        "agent_name",
        "agents_payload",
        "claude_native_agents_enabled",
        "prompt",
        "prompt_file_path",
        "base_instructions",
        "developer_instructions",
        "report_output_path",
        "user_turn_content",
        "skills",
        "reference_items",
        "task_cwd",
    }
)

_MANAGED_FLAG_ALIASES: dict[str, tuple[str, ...]] = {
    "--model": ("--model", "-m"),
    "--append-system-prompt": ("--append-system-prompt",),
    "--session": ("--session",),
    "--fork": ("--fork",),
    "--no-extensions": ("--no-extensions",),
    "--mode": ("--mode",),
    "-e": ("-e", "--extension"),
    "--no-skills": ("--no-skills",),
    "--no-context-files": ("--no-context-files",),
    "--no-prompt-templates": ("--no-prompt-templates",),
    "--session-dir": ("--session-dir",),
}

_EFFORT_TO_THINKING: dict[str, str] = {
    "low": "minimal",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
    "xhigh": "xhigh",
}


def _has_flag(args: Sequence[str], flag: str) -> bool:
    for token in args:
        if token == flag or token.startswith(f"{flag}="):
            return True
        if flag == "-e" and token.startswith("-e") and token != "-e":
            return True
    return False


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
        "Pi projection known managed flag %s also present in extra_args; "
        "user tail value wins by last-wins semantics",
        managed_flag,
    )


def _reject_mode_collisions(passthrough_tail: tuple[str, ...]) -> None:
    if any(_has_flag(passthrough_tail, alias) for alias in _MANAGED_FLAG_ALIASES["--mode"]):
        raise ValueError(
            "Pi harness owns --mode and always launches in RPC mode; "
            "remove --mode from passthrough extra_args"
        )


def _reject_extension_collisions(passthrough_tail: tuple[str, ...]) -> None:
    if any(
        _has_flag(passthrough_tail, alias)
        for alias in _MANAGED_FLAG_ALIASES["--no-extensions"]
    ):
        raise ValueError(
            "Pi harness owns extension loading for RPC launches; "
            "remove --no-extensions from passthrough extra_args"
        )
    if any(_has_flag(passthrough_tail, alias) for alias in _MANAGED_FLAG_ALIASES["-e"]):
        raise ValueError(
            "Pi harness owns extension loading for RPC launches; "
            "remove -e/--extension from passthrough extra_args"
        )


def _reject_session_dir_collisions(passthrough_tail: tuple[str, ...]) -> None:
    if any(
        _has_flag(passthrough_tail, alias)
        for alias in _MANAGED_FLAG_ALIASES["--session-dir"]
    ):
        raise ValueError(
            "Pi harness owns --session-dir for Meridian-managed session isolation; "
            "remove --session-dir from passthrough extra_args"
        )


def _project_model_arg(spec: ResolvedLaunchSpec) -> str | None:
    model = (spec.model or "").strip()
    if not model:
        return None

    thinking = _EFFORT_TO_THINKING.get((spec.effort or "").strip().lower())
    if thinking:
        return f"{model}:{thinking}"
    return model


def _default_pi_session_dir() -> str:
    return str(resolve_pi_spawn_session_root())


def project_pi_spec_to_cli_args(
    spec: ResolvedLaunchSpec,
    *,
    base_command: tuple[str, ...],
) -> list[str]:
    """Project one ``ResolvedLaunchSpec`` into an ordered Pi RPC command list."""

    command: list[str] = list(base_command)

    model_arg = _project_model_arg(spec)
    if model_arg is not None:
        command.extend(("--model", model_arg))

    if spec.appended_system_prompt:
        command.extend(("--append-system-prompt", spec.appended_system_prompt))

    continue_session_id = (spec.continue_session_id or "").strip()
    has_continue_session = bool(continue_session_id)
    has_continue_fork = has_continue_session and spec.continue_fork

    passthrough_tail = spec.extra_args
    _reject_mode_collisions(passthrough_tail)
    _reject_extension_collisions(passthrough_tail)
    _reject_session_dir_collisions(passthrough_tail)

    _log_collision_if_needed(
        managed_flag="--model",
        has_managed_value=model_arg is not None,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--append-system-prompt",
        has_managed_value=bool(spec.appended_system_prompt),
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
    _log_collision_if_needed(
        managed_flag="--no-extensions",
        has_managed_value=not spec.load_all_pi_extensions,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="-e",
        has_managed_value=True,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--no-skills",
        has_managed_value=True,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--no-context-files",
        has_managed_value=True,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--no-prompt-templates",
        has_managed_value=True,
        passthrough_tail=passthrough_tail,
    )
    _log_collision_if_needed(
        managed_flag="--session-dir",
        has_managed_value=True,
        passthrough_tail=passthrough_tail,
    )

    if has_continue_session:
        if has_continue_fork:
            command.extend(("--fork", continue_session_id))
        else:
            command.extend(("--session", continue_session_id))

    command.extend(("--session-dir", _default_pi_session_dir()))

    if not spec.load_all_pi_extensions:
        command.append("--no-extensions")

    command.extend(
        (
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
        )
    )

    for extension_entrypoint in spec.pi_extension_entrypoints:
        command.extend(("-e", extension_entrypoint))

    command.extend(resolve_permission_flags(spec.permission_resolver, HarnessId.PI))
    command.extend(passthrough_tail)

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
    "project_pi_spec_to_cli_args",
]
