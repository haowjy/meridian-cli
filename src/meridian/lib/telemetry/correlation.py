"""Lifecycle correlation context helpers for structured logging."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import structlog

_CANONICAL_CORRELATION_FIELDS: tuple[str, ...] = (
    "spawn_id",
    "parent_id",
    "chat_id",
    "work_id",
    "agent",
    "model",
    "harness",
    "lifecycle_operation",
    "lifecycle_event",
    "terminal_status",
    "terminal_origin",
    "failure_category",
)


@dataclass(frozen=True)
class LifecycleCorrelation:
    """Canonical correlation fields for spawn lifecycle operations."""

    spawn_id: str | None = None
    parent_id: str | None = None
    chat_id: str | None = None
    work_id: str | None = None
    agent: str | None = None
    model: str | None = None
    harness: str | None = None
    lifecycle_operation: str | None = None
    lifecycle_event: str | None = None
    terminal_status: str | None = None
    terminal_origin: str | None = None
    failure_category: str | None = None

    def to_context(self) -> dict[str, str]:
        """Return only populated correlation fields for structlog binding."""

        context: dict[str, str] = {}
        for field_name in _CANONICAL_CORRELATION_FIELDS:
            value = getattr(self, field_name)
            if value is not None and str(value).strip():
                context[field_name] = str(value)
        return context


@contextmanager
def bind_lifecycle_correlation(
    correlation: LifecycleCorrelation,
) -> Generator[LifecycleCorrelation, None, None]:
    """Bind lifecycle correlation fields for the current synchronous scope."""

    tokens = structlog.contextvars.bind_contextvars(**correlation.to_context())
    try:
        yield correlation
    finally:
        structlog.contextvars.reset_contextvars(**tokens)

def lifecycle_correlation_from_mapping(
    values: Mapping[str, str | None] | None = None,
    **kwargs: str | None,
) -> LifecycleCorrelation:
    """Build correlation fields from sparse values."""

    merged: dict[str, str | None] = {}
    if values is not None:
        merged.update(values)
    merged.update(kwargs)
    return LifecycleCorrelation(**merged)


__all__ = [
    "LifecycleCorrelation",
    "bind_lifecycle_correlation",
    "lifecycle_correlation_from_mapping",
]
