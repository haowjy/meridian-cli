"""Permission-flag projection helpers shared by harness command projectors."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from meridian.lib.core.types import HarnessId
from meridian.lib.launch.launch_types import PermissionResolver
from meridian.lib.safety.permissions import PermissionConfig


def _coerce_permission_flags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, tuple):
        tuple_tokens = cast("tuple[object, ...]", raw)
        return tuple(str(token) for token in tuple_tokens)
    if isinstance(raw, list):
        list_tokens = cast("list[object]", raw)
        return tuple(str(token) for token in list_tokens)
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Iterable):
        iterable_tokens = cast("Iterable[object]", raw)
        return tuple(str(token) for token in iterable_tokens)
    raise TypeError(f"Permission resolver flags must be iterable, got {type(raw).__name__}")


def _permission_flags_for_harness(
    *,
    harness_id: HarnessId,
    config: PermissionConfig,
) -> tuple[str, ...]:
    if harness_id == HarnessId.PI:
        # Pi permissions are mediated through extension UI hooks, not CLI flags.
        return ()

    # Codex projects approval and sandbox itself (``project_codex_common``).
    # New harness families that need approval/sandbox projection belong here.
    if harness_id != HarnessId.CLAUDE:
        return ()
    if config.approval in ("yolo", "never"):
        return ("--dangerously-skip-permissions",)
    if config.approval == "auto":
        return ("--permission-mode", "acceptEdits")
    if config.approval == "confirm":
        return ("--permission-mode", "default")
    return ()


def _strip_claude_tool_flags(flags: tuple[str, ...]) -> tuple[str, ...]:
    filtered: list[str] = []
    index = 0
    while index < len(flags):
        token = flags[index]
        if token in {"--allowedTools", "--disallowedTools"}:
            index += 2
            continue
        filtered.append(token)
        index += 1
    return tuple(filtered)


def resolve_permission_flags(
    permission_resolver: PermissionResolver,
    harness_id: HarnessId,
) -> tuple[str, ...]:
    """Resolve projected permission flags for one harness."""

    base_flags = list(
        _permission_flags_for_harness(harness_id=harness_id, config=permission_resolver.config)
    )
    resolver_flags = _coerce_permission_flags(permission_resolver.resolve_flags())
    if harness_id != HarnessId.CLAUDE:
        resolver_flags = _strip_claude_tool_flags(resolver_flags)
    base_flags.extend(resolver_flags)
    return tuple(base_flags)


__all__ = ["resolve_permission_flags"]
