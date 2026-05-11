"""Process-wide lifecycle observer registry."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import asdict
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from meridian.lib.core.telemetry import LifecycleEvent


logger = logging.getLogger(__name__)


class LifecycleObserverTier(Enum):
    """Observer tier determines exception handling and ordering."""

    DIAGNOSTIC = "diagnostic"  # best-effort, exceptions logged and swallowed
    POLICY = "policy"  # control path, exceptions propagate


class LifecycleObserver(Protocol):
    """Observer of spawn lifecycle events."""

    def on_event(self, event: LifecycleEvent) -> None:
        """Handle a lifecycle event.

        Called synchronously. Async observers should wrap their async
        dispatch in asyncio.create_task() inside this method.
        """
        ...


class DebugTraceObserver:
    """Print lifecycle events as JSON to stderr when MERIDIAN_DEBUG=1."""

    def on_event(self, event: LifecycleEvent) -> None:
        """Emit a JSON line for debug tracing when enabled."""
        if os.environ.get("MERIDIAN_DEBUG") != "1":
            return
        data = asdict(event)
        data["ts"] = event.ts.isoformat()
        print(json.dumps(data), file=sys.stderr)


_GLOBAL_OBSERVERS: list[tuple[LifecycleObserver, LifecycleObserverTier]] = []
_GLOBAL_OBSERVERS_LOCK = threading.Lock()
_debug_trace_registered = False


def register_observer(
    observer: LifecycleObserver,
    tier: LifecycleObserverTier = LifecycleObserverTier.DIAGNOSTIC,
) -> None:
    """Register a process-wide lifecycle observer."""
    with _GLOBAL_OBSERVERS_LOCK:
        _GLOBAL_OBSERVERS.append((observer, tier))


def register_debug_trace_observer() -> None:
    """Register the process-wide debug trace observer once."""
    global _debug_trace_registered
    with _GLOBAL_OBSERVERS_LOCK:
        if _debug_trace_registered:
            return
        _GLOBAL_OBSERVERS.append((DebugTraceObserver(), LifecycleObserverTier.DIAGNOSTIC))
        _debug_trace_registered = True


def notify_observers(event: LifecycleEvent) -> None:
    """Dispatch a lifecycle event to process-wide observers by tier."""
    with _GLOBAL_OBSERVERS_LOCK:
        observers = tuple(_GLOBAL_OBSERVERS)
    policy_observers = [
        observer for observer, tier in observers if tier == LifecycleObserverTier.POLICY
    ]
    diagnostic_observers = [
        observer for observer, tier in observers if tier == LifecycleObserverTier.DIAGNOSTIC
    ]

    for observer in policy_observers:
        observer.on_event(event)

    for observer in diagnostic_observers:
        try:
            observer.on_event(event)
        except Exception:
            logger.exception("Diagnostic observer failed for event %s", event.event)
