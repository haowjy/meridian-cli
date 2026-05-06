"""Command assembly helpers for primary launches."""

from __future__ import annotations

from typing import cast

from meridian.lib.core.types import ModelId
from meridian.lib.harness.adapter import SpawnParams, SubprocessHarness
from meridian.lib.harness.launch_spec import (
    ClaudeLaunchSpec,
    CodexLaunchSpec,
    OpenCodeLaunchSpec,
)
from meridian.lib.harness.projections.project_claude import (
    project_claude_spec_to_cli_args,
)
from meridian.lib.harness.projections.project_codex_subprocess import (
    project_codex_spec_to_cli_args,
)
from meridian.lib.harness.projections.project_opencode_subprocess import (
    project_opencode_spec_to_cli_args,
)
from meridian.lib.launch.launch_types import PermissionResolver, ResolvedLaunchSpec

from .run_inputs import (
    ResolvedRunInputs,
    coerce_resolved_run_inputs,
    to_spawn_params,
)


def normalize_system_prompt_passthrough_args(
    passthrough_args: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract system-prompt passthroughs and return args without prompt duplicates."""

    cleaned: list[str] = []
    prompt_fragments: list[str] = []
    index = 0
    while index < len(passthrough_args):
        token = passthrough_args[index]

        if token in {"--append-system-prompt", "--system-prompt"}:
            if index + 1 >= len(passthrough_args):
                raise ValueError(f"{token} requires a value")
            prompt_fragments.append(passthrough_args[index + 1])
            index += 2
            continue

        if token.startswith("--append-system-prompt="):
            prompt_fragments.append(token.partition("=")[2])
            index += 1
            continue

        if token.startswith("--system-prompt="):
            prompt_fragments.append(token.partition("=")[2])
            index += 1
            continue

        cleaned.append(token)
        index += 1

    return tuple(cleaned), tuple(prompt_fragments)


def resolve_launch_spec_stage(
    *,
    adapter: SubprocessHarness,
    run_inputs: ResolvedRunInputs | SpawnParams,
    perms: PermissionResolver,
) -> ResolvedLaunchSpec:
    """Stage-owned adapter callsite for `resolve_launch_spec`.

    Reference delivery flow (intentional split):
    1. `build_launch_context()` loads `reference_items` into `ResolvedRunInputs`.
    2. `to_spawn_params()` intentionally drops `reference_items` because most harnesses
       never consume them.
    3. This stage selectively re-attaches `reference_items` onto the resolved spec only
       when the active adapter advertises native file injection support and the spec model
       exposes a `reference_items` field.

    This keeps generic `SpawnParams` stable while still making native-injection data
    available to any harness projection that explicitly supports it.
    """

    spec = adapter.resolve_launch_spec(to_spawn_params(run_inputs), perms)

    # If harness supports native file injection and we have reference_items,
    # update the spec with them (only for specs that have the field)
    if (
        isinstance(run_inputs, ResolvedRunInputs)
        and run_inputs.reference_items
        and adapter.capabilities.supports_native_file_injection
        and hasattr(spec, "reference_items")
    ):
        spec = spec.model_copy(update={"reference_items": run_inputs.reference_items})

    return spec


def _projected_spec_to_run_inputs(
    *,
    run_inputs: ResolvedRunInputs,
    projected_spec: ResolvedLaunchSpec,
) -> ResolvedRunInputs:
    projected_model = projected_spec.model.strip() if projected_spec.model else ""
    return run_inputs.model_copy(
        update={
            "prompt": projected_spec.prompt,
            "model": ModelId(projected_model) if projected_model else None,
            "effort": projected_spec.effort,
            "extra_args": projected_spec.extra_args,
            "projected_roots": projected_spec.projected_roots,
            "continue_harness_session_id": projected_spec.continue_session_id,
            "continue_fork": projected_spec.continue_fork,
            "interactive": projected_spec.interactive,
            "mcp_tools": projected_spec.mcp_tools,
        }
    )


def _base_command(adapter: SubprocessHarness, attribute: str) -> tuple[str, ...]:
    value = getattr(adapter, attribute, ())
    if not isinstance(value, tuple):
        return ()
    return cast("tuple[str, ...]", value)


def _project_known_spec_argv(
    *,
    adapter: SubprocessHarness,
    projected_spec: ResolvedLaunchSpec,
) -> tuple[str, ...] | None:
    if isinstance(projected_spec, ClaudeLaunchSpec):
        base_command = _base_command(adapter, "PRIMARY_BASE_COMMAND")
        if not projected_spec.interactive:
            base_command = (*_base_command(adapter, "BASE_COMMAND"), "-")
        if base_command:
            return tuple(
                project_claude_spec_to_cli_args(
                    projected_spec,
                    base_command=base_command,
                )
            )
        return None

    if isinstance(projected_spec, CodexLaunchSpec):
        base_command = (
            _base_command(adapter, "PRIMARY_BASE_COMMAND")
            if projected_spec.interactive
            else _base_command(adapter, "BASE_COMMAND")
        )
        if base_command:
            return tuple(
                project_codex_spec_to_cli_args(
                    projected_spec,
                    base_command=base_command,
                )
            )
        return None

    if isinstance(projected_spec, OpenCodeLaunchSpec):
        base_command = (
            _base_command(adapter, "PRIMARY_BASE_COMMAND")
            if projected_spec.interactive
            else _base_command(adapter, "BASE_COMMAND")
        )
        if base_command:
            return tuple(
                project_opencode_spec_to_cli_args(
                    projected_spec,
                    base_command=base_command,
                )
            )
        return None

    return None


def build_launch_argv(
    *,
    adapter: SubprocessHarness,
    run_inputs: ResolvedRunInputs | SpawnParams,
    perms: PermissionResolver,
    projected_spec: ResolvedLaunchSpec,
) -> tuple[str, ...]:
    """Stage-owned adapter callsite for `build_command` from projected spec."""

    known_projection = _project_known_spec_argv(
        adapter=adapter,
        projected_spec=projected_spec,
    )
    if known_projection is not None:
        return known_projection

    normalized_inputs = coerce_resolved_run_inputs(run_inputs)
    argv_inputs = _projected_spec_to_run_inputs(
        run_inputs=normalized_inputs,
        projected_spec=projected_spec,
    )
    return tuple(adapter.build_command(to_spawn_params(argv_inputs), perms))


__all__ = [
    "build_launch_argv",
    "normalize_system_prompt_passthrough_args",
    "resolve_launch_spec_stage",
]
