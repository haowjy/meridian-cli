"""Command assembly helpers for primary launches."""

from __future__ import annotations

from typing import cast

from meridian.lib.harness.adapter import SpawnParams, SubprocessHarness
from meridian.lib.harness.bundle import project_subprocess_spec
from meridian.lib.harness.ids import HarnessId
from meridian.lib.launch.launch_types import PermissionResolver, ResolvedLaunchSpec

from .run_inputs import ResolvedRunInputs, to_spawn_params


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


def _tuple_attr(adapter: SubprocessHarness, attribute: str) -> tuple[str, ...]:
    value = getattr(adapter, attribute, ())
    if not isinstance(value, tuple):
        return ()
    return cast("tuple[str, ...]", value)


def _base_command(adapter: SubprocessHarness, *, interactive: bool) -> tuple[str, ...]:
    primary_base_command = _tuple_attr(adapter, "PRIMARY_BASE_COMMAND")
    subprocess_base_command = _tuple_attr(adapter, "BASE_COMMAND")

    if interactive:
        return primary_base_command

    if adapter.id is HarnessId.CLAUDE:
        return (*subprocess_base_command, "-")
    return subprocess_base_command


def build_launch_argv(
    *,
    adapter: SubprocessHarness,
    run_inputs: ResolvedRunInputs | SpawnParams,
    perms: PermissionResolver,
    projected_spec: ResolvedLaunchSpec,
) -> tuple[str, ...]:
    """Stage-owned subprocess argv assembly from one resolved launch spec."""

    _ = run_inputs, perms
    return tuple(
        project_subprocess_spec(
            adapter.id,
            projected_spec,
            base_command=_base_command(adapter, interactive=projected_spec.interactive),
        )
    )


__all__ = [
    "build_launch_argv",
    "normalize_system_prompt_passthrough_args",
    "project_subprocess_spec",
    "resolve_launch_spec_stage",
]
