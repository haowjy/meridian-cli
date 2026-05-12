"""Shared authority-scoping helpers for manual hooks CLI commands."""

from __future__ import annotations

import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager

from meridian.lib.core.depth import is_nested_meridian_process

_MANUAL_HOOK_ENV_OVERRIDES: tuple[str, ...] = (
    "MERIDIAN_PROJECT_DIR",
    "MERIDIAN_RUNTIME_DIR",
)


def is_manual_hooks_invocation(argv: Sequence[str]) -> bool:
    """Return whether argv resolves to manual hooks list/run behavior."""

    if not argv or argv[0] != "hooks":
        return False
    if len(argv) == 1:
        return True
    return argv[1] in {"list", "run"}


def should_suppress_manual_hook_authority(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether inherited project/runtime env should be ignored.

    We only suppress env overrides for nested manual hooks invocations so
    top-level callers can still target explicit project/runtime roots via env.
    """

    source = os.environ if env is None else env
    if argv is not None and not is_manual_hooks_invocation(argv):
        return False
    if not is_nested_meridian_process(source):
        return False
    return any((source.get(name) or "").strip() for name in _MANUAL_HOOK_ENV_OVERRIDES)


@contextmanager
def manual_hook_authority_scope(*, suppress: bool) -> Generator[None, None, None]:
    """Temporarily clear inherited hook authority env when required."""

    if not suppress:
        yield
        return

    previous = {name: os.environ.pop(name, None) for name in _MANUAL_HOOK_ENV_OVERRIDES}
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is not None:
                os.environ[name] = value


__all__ = [
    "is_manual_hooks_invocation",
    "manual_hook_authority_scope",
    "should_suppress_manual_hook_authority",
]
