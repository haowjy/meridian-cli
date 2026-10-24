"""Claude command-line projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections._prompt_arg import check_prompt_argument
from meridian.lib.harness.projections.compaction import project_claude_autocompact_args
from meridian.lib.harness.projections.permissions_claude import (
    ClaudeSettings,
    compile_claude_permissions,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)

_PROJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "agent_name",
        "agents_payload",
        "appended_system_prompt",
        "continue_fork",
        "continue_session_id",
        "effort",
        "autocompact",
        "autocompact_pct",
        "extra_args",
        "interactive",
        "projected_roots",
        "model",
        "native_config",
        "permission_policy",
        "prompt",
        "prompt_file_path",
        "claude_runtime",
        "codex_runtime",
        "user_turn_content",
    }
)

_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "native_identity",
        "base_instructions",
        "developer_instructions",
        "harness",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
        "opencode_version",
        "reference_items",
        "report_output_path",
        "skills",
        "task_cwd",
    }
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
    if managed_flag == "--append-system-prompt":
        logger.warning(message, managed_flag)
        return
    logger.debug(message, managed_flag)


def _settings_json(settings: ClaudeSettings) -> str:
    payload: dict[str, object] = {}
    permissions: dict[str, object] = {}
    if settings.allow:
        permissions["allow"] = list(settings.allow)
    if settings.deny:
        permissions["deny"] = list(settings.deny)
    if settings.ask:
        permissions["ask"] = list(settings.ask)
    if permissions:
        payload["permissions"] = permissions
    if settings.sandbox is not None:
        sandbox_settings = settings.sandbox
        sandbox: dict[str, object] = {}
        if sandbox_settings.enabled is not None:
            sandbox["enabled"] = sandbox_settings.enabled
        if sandbox_settings.enabled is True:
            sandbox["allowUnsandboxedCommands"] = False
            sandbox["autoAllowBashIfSandboxed"] = sandbox_settings.auto_allow_bash
            if sandbox_settings.deny_write:
                sandbox["filesystem"] = {"denyWrite": list(sandbox_settings.deny_write)}
        if sandbox_settings.excluded_commands:
            sandbox["excludedCommands"] = list(sandbox_settings.excluded_commands)
        payload["sandbox"] = sandbox
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


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
    command.extend(project_claude_autocompact_args(spec.autocompact))
    if spec.agent_name:
        command.extend(("--agent", str(spec.agent_name)))
    if (
        spec.native_identity
        and spec.native_identity.operation == "create"
        and spec.native_identity.session_id
    ):
        command.extend(("--session-id", spec.native_identity.session_id))

    permissions, _ = compile_claude_permissions(spec)
    passthrough_tail = permissions.passthrough_args

    for plugin_dir in spec.claude_runtime.plugin_dirs:
        command.extend(("--plugin-dir", plugin_dir))

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
        managed_flag="--append-system-prompt",
        has_managed_value=bool(spec.appended_system_prompt),
        passthrough_tail=passthrough_tail,
    )

    command.extend(passthrough_tail)

    # Permission projection comes after passthrough. Claude keeps only the last
    # --settings (and --permission-mode), so the managed settings replace a raw one; the
    # compile consumed raw tool allow/deny flags and omits its own mode when passthrough
    # sets one. Passthrough can still widen the profile (--dangerously-skip-permissions,
    # --permission-mode, --allow-dangerously-skip-permissions): those flags stay, and the
    # report says so.
    if permissions.skip_permission_prompts:
        command.append("--dangerously-skip-permissions")
    if permissions.permission_mode:
        command.extend(("--permission-mode", permissions.permission_mode))
    if permissions.permission_prompts:
        command.extend(("--permission-prompts", permissions.permission_prompts))
    if permissions.restrict_tools:
        command.extend(("--tools", ",".join(permissions.available_tools)))
    if permissions.disallowed_tools:
        command.extend(("--disallowedTools", ",".join(permissions.disallowed_tools)))
    if not permissions.settings.empty:
        command.extend(("--settings", _settings_json(permissions.settings)))

    # For interactive Claude launches, add user-turn content as positional argument
    # This routes USER_TASK_PROMPT and TASK_CONTEXT to the user-turn channel
    # instead of --append-system-prompt (spec S-2a)
    if spec.interactive and spec.user_turn_content:
        command.extend(("--", check_prompt_argument(spec.user_turn_content)))

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
