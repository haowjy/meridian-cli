"""The one rule for when a session's prompt cache goes cold."""

from __future__ import annotations

from meridian.lib.state.idle_store import IdleState

# Estimated fallbacks when notification state is absent or has been garbage-collected.
_DEFAULT_CACHE_TTL_SECONDS = {"claude": 300, "codex": 1800, "opencode": 300, "pi": 300, "agy": 1800}


def cache_deadline(
    harness: str,
    anchor_s: float,
    state: IdleState | None,
) -> tuple[float, bool] | None:
    """Epoch seconds when the cache cools after ``anchor_s``, and whether that is a guess.

    Uses the TTL the idle sensor recorded, else the harness default (always a guess);
    ``None`` for a harness with neither.
    """

    if state is not None and state.ttl_seconds:
        return anchor_s + state.ttl_seconds, state.ttl_estimated
    default = _DEFAULT_CACHE_TTL_SECONDS.get(harness)
    return (anchor_s + default, True) if default is not None else None
