"""Pi-specific idle policy observations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.harness.idle_types import CacheTtl

# OpenAI's "24h" retention typically lasts 30 minutes, so schedule conservatively.
_OPENAI_LONG_RETENTION_TTL_SECONDS = 1800
# Unknown backend TTL; mirror the Codex harness's 1800-second estimate.
_OPENAI_CODEX_ESTIMATED_TTL_SECONDS = 1800
_PROVIDER_TTLS: dict[str, dict[bool, CacheTtl]] = {
    "anthropic": {
        False: CacheTtl(300, estimated=False),
        True: CacheTtl(3600, estimated=False),
    },
    "openai": {
        True: CacheTtl(_OPENAI_LONG_RETENTION_TTL_SECONDS, estimated=True),
    },
    "openai-codex": {
        False: CacheTtl(_OPENAI_CODEX_ESTIMATED_TTL_SECONDS, estimated=True),
        True: CacheTtl(_OPENAI_CODEX_ESTIMATED_TTL_SECONDS, estimated=True),
    },
}


def detect_ttl(
    *,
    session_id: str,
    native_store: Path | None,
    provider: str | None,
    env: Mapping[str, str],
) -> CacheTtl | None:
    """Resolve Pi cache retention and confidence from its provider and mode."""

    _ = session_id, native_store
    normalized_provider = (provider or "").strip().lower()
    long_retention = env.get("PI_CACHE_RETENTION", "").strip().lower() == "long"
    return _PROVIDER_TTLS.get(normalized_provider, {}).get(long_retention)


__all__ = ["detect_ttl"]
