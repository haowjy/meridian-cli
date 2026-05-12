"""Shared child-env contract for spawn and launch boundaries.

This module defines the canonical ``MERIDIAN_*`` key surface that callers may
propagate into child processes.
"""

import re
from collections.abc import Mapping

# Authoritative ``MERIDIAN_*`` key allowlist for child-process propagation.
# Must stay aligned with ResolvedContext.child_env_overrides().
ALLOWED_CHILD_ENV_KEYS: frozenset[str] = frozenset(
    {
        "MERIDIAN_SPAWN_ID",
        "MERIDIAN_PARENT_SPAWN_ID",
        "MERIDIAN_PROJECT_DIR",
        "MERIDIAN_RUNTIME_DIR",
        "MERIDIAN_DEPTH",
        "MERIDIAN_CHAT_ID",
        "MERIDIAN_ACTIVE_WORK_ID",
        "MERIDIAN_ACTIVE_WORK_DIR",
    }
)

_CONTEXT_DIR_PATTERN = re.compile(r"^MERIDIAN_CONTEXT_[A-Z][A-Z0-9_]*_DIR$")


def validate_child_env_keys(overrides: Mapping[str, str]) -> None:
    """Raise if overrides contain unexpected MERIDIAN_* keys.

    Unexpected means: starts with ``MERIDIAN_`` but is not in
    :data:`ALLOWED_CHILD_ENV_KEYS`.
    """
    for key in overrides:
        if not key.startswith("MERIDIAN_"):
            continue
        if key in ALLOWED_CHILD_ENV_KEYS:
            continue
        if _CONTEXT_DIR_PATTERN.match(key):
            continue
        raise RuntimeError(f"Unexpected MERIDIAN_* key in child env: {key}")


__all__ = [
    "ALLOWED_CHILD_ENV_KEYS",
    "validate_child_env_keys",
]
