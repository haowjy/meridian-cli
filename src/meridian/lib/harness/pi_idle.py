"""Pi-specific idle policy observations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.harness.idle_types import CacheTtl, CompactCosts

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


def compact_costs(
    *, provider: str | None, ttl_seconds: int | None
) -> CompactCosts | None:
    """Return Pi route economics when its provider has a known TTL."""

    normalized = (provider or "").strip().lower()
    if normalized == "anthropic":
        # Pi's own summary prompt uses cacheRetention="none", so its input is
        # cold. R is keepRecentTokens (20k) plus the measured summary estimate.
        return CompactCosts(
            w=2.0 if ttl_seconds == 3600 else 1.25,
            c=1.0,
            u=0,
            o=5.0,
            summary_tokens=4_000,
            residue_tokens=22_000,
        )
    if normalized in {"openai", "openai-codex"}:
        # Two openai-codex probes had cacheRead=0. GPT-5.x output is priced at
        # roughly 8x input; the same 20k recent-token retention applies.
        return CompactCosts(
            w=1.0,
            c=1.0,
            u=0,
            o=8.0,
            summary_tokens=4_000,
            residue_tokens=22_000,
        )
    return None


__all__ = ["compact_costs", "detect_ttl"]
