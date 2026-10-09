"""Pi-specific idle policy observations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def detect_ttl(
    provider: str | None,
    env: Mapping[str, str],
    *,
    session_id: str = "",
    cwd: Path | None = None,
) -> int | None:
    """Resolve Pi cache retention from its provider and retention mode."""

    _ = session_id, cwd
    normalized_provider = (provider or "").strip().lower()
    long_retention = env.get("PI_CACHE_RETENTION", "").strip().lower() == "long"
    if normalized_provider == "anthropic":
        return 3600 if long_retention else 300
    if normalized_provider == "openai" and long_retention:
        return 86400
    return None


def idle_env_facts(env: Mapping[str, str]) -> dict[str, object]:
    """Read Pi's environment-visible cache-retention fact."""

    return {"cache_retention": env.get("PI_CACHE_RETENTION")}


__all__ = ["detect_ttl", "idle_env_facts"]
