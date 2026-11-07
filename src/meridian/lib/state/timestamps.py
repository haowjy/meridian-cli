from __future__ import annotations

from datetime import UTC, datetime


def iso_timestamp_to_epoch(timestamp: str | None) -> float | None:

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


def epoch_to_iso_timestamp(epoch: float) -> str:

    return (
        datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
