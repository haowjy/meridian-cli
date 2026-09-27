"""Synchronous event hooks behind one isolation boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterable

import structlog


def run_event_hooks[EventT](hooks: Iterable[Callable[[EventT], None]], event: EventT) -> None:
    """One isolation boundary for facts and side effects, independent of persistence."""
    for hook in hooks:
        try:
            hook(event)
        except Exception:
            structlog.get_logger(__name__).exception("event_hook_failed")


__all__ = ["run_event_hooks"]
