"""Pi-specific idle policy observations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.harness.idle_types import CacheTtl

# OpenAI's "24h" retention typically lasts 30 minutes, so schedule conservatively.
_OPENAI_LONG_RETENTION_TTL_SECONDS = 1800


def detect_ttl(
    provider: str | None,
    env: Mapping[str, str],
    *,
    session_id: str = "",
    cwd: Path | None = None,
) -> CacheTtl | None:
    """Resolve Pi cache retention and confidence from its provider and mode."""

    _ = session_id, cwd
    normalized_provider = (provider or "").strip().lower()
    long_retention = env.get("PI_CACHE_RETENTION", "").strip().lower() == "long"
    if normalized_provider == "anthropic":
        return CacheTtl(3600 if long_retention else 300, estimated=False)
    if normalized_provider == "openai" and long_retention:
        return CacheTtl(_OPENAI_LONG_RETENTION_TTL_SECONDS, estimated=True)
    return None

__all__ = ["detect_ttl"]
