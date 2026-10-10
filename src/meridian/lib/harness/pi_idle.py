"""Pi-specific idle policy observations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

# OpenAI's "24h" retention typically lasts 30 minutes, so schedule conservatively.
_OPENAI_LONG_RETENTION_TTL_SECONDS = 1800


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
        return _OPENAI_LONG_RETENTION_TTL_SECONDS
    return None

__all__ = ["detect_ttl"]
