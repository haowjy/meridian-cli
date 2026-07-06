"""Ops-layer helpers for record-backed Meridian depth."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.state.depth_resolution import resolve_effective_meridian_depth


def effective_meridian_depth(
    env: Mapping[str, str],
    *,
    runtime_root: Path | None,
) -> int | None:
    """Return record-backed depth when runtime root is known, else ``None``."""

    if runtime_root is None:
        return None
    return resolve_effective_meridian_depth(env, runtime_root=runtime_root)


def with_record_backed_depth(
    resolved: ResolvedContext,
    env: Mapping[str, str] | None = None,
) -> ResolvedContext:
    """Return ``resolved`` with spawn-record depth applied when available."""

    if resolved.runtime_root is None:
        return resolved
    source = os.environ if env is None else env
    depth = resolve_effective_meridian_depth(source, runtime_root=resolved.runtime_root)
    if depth == resolved.depth:
        return resolved
    return replace(resolved, depth=depth)


def with_record_backed_runtime_context(
    ctx: RuntimeContext,
    env: Mapping[str, str] | None = None,
) -> RuntimeContext:
    """Return ``ctx`` with spawn-record depth applied when available."""

    if ctx.runtime_root is None:
        return ctx
    source = os.environ if env is None else env
    depth = resolve_effective_meridian_depth(source, runtime_root=ctx.runtime_root)
    if depth == ctx.depth:
        return ctx
    return ctx.model_copy(update={"depth": depth})


__all__ = [
    "effective_meridian_depth",
    "with_record_backed_depth",
    "with_record_backed_runtime_context",
]
