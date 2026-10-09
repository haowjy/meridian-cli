"""OpenCode-specific idle policy observations."""

from __future__ import annotations

from collections.abc import Mapping

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def idle_env_facts(env: Mapping[str, str]) -> dict[str, object]:
    """Read OpenCode facts that are visible only in the process environment."""

    raw = env.get("OPENCODE_DISABLE_AUTOCOMPACT", "")
    return {"harness_autocompact_off": raw.strip().lower() in _TRUTHY}


__all__ = ["idle_env_facts"]
