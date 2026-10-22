"""Codex subprocess command projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import json

from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions
from meridian.lib.harness.projections.project_codex_common import (
    CODEX_DISABLE_BUILTIN_AGENT_FLAGS,
    project_codex_mcp_config_flags,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

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
        "report_output_path",
        "base_instructions",
        "developer_instructions",
        "user_turn_content",
    }
)

_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "native_identity",
        "agent_name",
        "agents_payload",
        "claude_runtime",
        "appended_system_prompt",
        "harness",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
        "opencode_version",
        "prompt_file_path",
        "reference_items",
        "skills",
        "task_cwd",
    }
)


def project_codex_spec_to_cli_args(
    spec: ResolvedLaunchSpec,
    *,
    base_command: tuple[str, ...],
) -> list[str]:
    """Project one ``ResolvedLaunchSpec`` into an ordered subprocess command list."""

    command: list[str] = list(base_command)
    prompt_parts = (
        spec.base_instructions,
        spec.developer_instructions,
        spec.prompt,
    )
    final_prompt = "\n\n".join(part.strip() for part in prompt_parts if part and part.strip())

    harness_session_id = (spec.continue_session_id or "").strip()
    if spec.interactive:
        guarded_prompt = final_prompt
        if guarded_prompt and not harness_session_id:
            guarded_prompt = f"{guarded_prompt}\n\nDO NOT DO ANYTHING. WAIT FOR USER INPUT."
    else:
        guarded_prompt = final_prompt

    if spec.model is not None:
        command.extend(("--model", spec.model))

    normalized_effort = (spec.effort or "").strip()
    if normalized_effort:
        command.extend(("-c", f"model_reasoning_effort={json.dumps(normalized_effort)}"))

    permissions, _ = compile_codex_permissions(spec)
    if permissions.sandbox_mode is not None:
        command.extend(("--sandbox", permissions.sandbox_mode))
    if permissions.approval_policy is not None:
        command.extend(("-c", f"approval_policy={json.dumps(permissions.approval_policy)}"))
    command.extend(project_codex_mcp_config_flags(spec.mcp_tools))

    # Codex ignores bundle tool grants. Enable the native Responses `web_search`
    # tool via the config knob (`-c tools.web_search=true`) rather than the `exec`-only
    # `--search` flag: the streaming transport launches `codex app-server`, which rejects
    # `--search` but accepts `-c key=value`. One mechanism works for both transports.
    if permissions.web_search:
        command.extend(("-c", "tools.web_search=true"))

    if harness_session_id:
        command.extend(("resume", harness_session_id))

    for root in spec.projected_roots:
        command.extend(("--add-dir", root.as_posix()))

    # Ahead of passthrough args so a later `-c` override still wins.
    if permissions.builtin_agents_disabled:
        command.extend(CODEX_DISABLE_BUILTIN_AGENT_FLAGS)
    command.extend(spec.extra_args)

    if not spec.interactive and spec.report_output_path:
        command.extend(("-o", spec.report_output_path))

    if guarded_prompt:
        command.append(guarded_prompt)

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
    "project_codex_spec_to_cli_args",
]
