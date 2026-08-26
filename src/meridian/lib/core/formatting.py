"""Shared text formatting primitives.

Centralizes column alignment and key-value rendering so that all
format_text() implementations produce consistent output.

This module lives in lib/core so it can be used by lib-layer result
models without reaching up into the CLI layer.
"""

from datetime import UTC, datetime


def tabular(rows: list[list[str]], sep: str = "  ") -> str:
    """Align columns by max width per column.

    >>> tabular([["r1", "done", "5.0s"], ["r20", "failed", "21.6s"]])
    'r1   done    5.0s\\nr20  failed  21.6s'
    """
    if not rows:
        return ""
    col_count = max(len(row) for row in rows)
    col_widths = [
        max((len(row[col]) if col < len(row) else 0) for row in rows) for col in range(col_count)
    ]
    lines: list[str] = []
    for row in rows:
        cells = [
            (row[col] if col < len(row) else "").ljust(col_widths[col]) for col in range(col_count)
        ]
        lines.append(sep.join(cells).rstrip())
    return "\n".join(lines)


def kv_block(pairs: list[tuple[str, str | None]]) -> str:
    """Render key: value pairs, skipping None values.

    >>> kv_block([("Spawn", "r1"), ("Status", "done"), ("Cost", None)])
    'Spawn: r1\\nStatus: done'
    """
    return "\n".join(f"{k}: {v}" for k, v in pairs if v is not None)


def relative_time(iso_str: str) -> str:
    """Format an ISO timestamp as a compact relative time string."""

    try:
        normalized = iso_str.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        delta = max(0, int((datetime.now(UTC) - parsed).total_seconds()))
    except (AttributeError, TypeError, ValueError):
        return iso_str

    if delta < 60:
        return f"{delta}s ago"
    minutes = delta // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"
