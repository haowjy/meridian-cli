"""Parsing helpers for timestamps persisted in Meridian state files."""

from __future__ import annotations

from datetime import UTC, datetime


def iso_timestamp_to_epoch(timestamp: str | None) -> float | None:
    """Parse a persisted ISO timestamp to epoch seconds."""

    normalized = (timestamp or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()
