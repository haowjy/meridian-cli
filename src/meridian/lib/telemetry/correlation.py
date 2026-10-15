"""Lifecycle correlation context helpers for structured logging."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

import structlog

from meridian.lib.core.identity import identity_ids
from meridian.lib.core.types import RunId

_CANONICAL_CORRELATION_FIELDS: tuple[str, ...] = (
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

    run_id: RunId | None = None
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
        if self.run_id is not None:
            context.update(identity_ids(self.run_id))
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


__all__ = [
    "LifecycleCorrelation",
    "bind_lifecycle_correlation",
]
