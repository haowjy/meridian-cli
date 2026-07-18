"""Shared child-env contract for spawn and launch boundaries.

This module defines the canonical ``MERIDIAN_*`` key surface that callers may
propagate into child processes.
"""

from collections.abc import Mapping

from meridian.env_registry import (
    ALLOWED_CHILD_ENV_KEYS,
    is_allowed_child_env_name,
)


def validate_child_env_keys(overrides: Mapping[str, str]) -> None:
    """Raise if overrides contain unexpected MERIDIAN_* keys.

    Unexpected means: starts with ``MERIDIAN_`` but is not in
    :data:`ALLOWED_CHILD_ENV_KEYS`.
    """
    for key in overrides:
        normalized = key.upper()
        if not normalized.startswith(("MERIDIAN_", "_MERIDIAN_")):
            continue
        if is_allowed_child_env_name(normalized):
            continue
        raise RuntimeError(f"Unexpected Meridian key in child env: {key}")


__all__ = [
    "ALLOWED_CHILD_ENV_KEYS",
    "validate_child_env_keys",
]
