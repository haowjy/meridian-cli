"""Permission-flag projection helpers shared by harness command projectors."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import cast

from meridian.lib.core.types import HarnessId
from meridian.lib.launch.launch_types import PermissionResolver
from meridian.lib.safety.permissions import PermissionConfig

logger = logging.getLogger(__name__)


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
    # New harness families that need approval/sandbox projection belong here.
    if config.approval == "yolo":
        if harness_id == HarnessId.CLAUDE:
            return ("--dangerously-skip-permissions",)
        if harness_id == HarnessId.CODEX:
            return ("--dangerously-bypass-approvals-and-sandbox",)
        return ()

    if config.approval == "auto":
        if harness_id == HarnessId.CLAUDE:
            return ("--permission-mode", "acceptEdits")
        if harness_id == HarnessId.CODEX:
            return ("--full-auto",)
        return ()

    if config.approval == "confirm":
        if harness_id == HarnessId.CLAUDE:
            return ("--permission-mode", "default")
        if harness_id == HarnessId.CODEX:
            return ("--ask-for-approval", "untrusted")
        return ()

    if harness_id == HarnessId.CODEX and config.sandbox != "default":
        return ("--sandbox", config.sandbox)
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
        stripped = _strip_claude_tool_flags(resolver_flags)
        if harness_id == HarnessId.CODEX and stripped != resolver_flags:
            logger.debug("Codex projection ignored Claude tool permission flags.")
        resolver_flags = stripped
    base_flags.extend(resolver_flags)
    return tuple(base_flags)


__all__ = ["resolve_permission_flags"]
