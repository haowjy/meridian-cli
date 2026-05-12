"""Command assembly helpers for primary launches."""

from __future__ import annotations

from typing import cast

from meridian.lib.harness.adapter import SpawnParams, SubprocessHarness
from meridian.lib.harness.bundle import project_subprocess_spec
from meridian.lib.launch.launch_types import PermissionResolver, ResolvedLaunchSpec


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
    run_inputs: SpawnParams,
    perms: PermissionResolver,
) -> ResolvedLaunchSpec:
    """Stage-owned adapter callsite for `resolve_launch_spec`.

    Reference delivery flow: `SpawnParams.reference_items` is selectively
    re-attached onto the resolved spec only when the active adapter advertises
    native file injection support and the spec model exposes a `reference_items`
    field. All current adapters have `supports_native_file_injection=False` so
    the block below is a future-proofing hook.
    """

    spec = adapter.resolve_launch_spec(run_inputs, perms)

    # If harness supports native file injection and we have reference_items, attach them.
    if run_inputs.reference_items and adapter.capabilities.supports_native_file_injection:
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

    return primary_base_command if interactive else subprocess_base_command


def build_launch_argv(
    *,
    adapter: SubprocessHarness,
    run_inputs: SpawnParams,
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
