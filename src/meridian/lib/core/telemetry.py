"""Lifecycle event model and compatibility observer exports."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meridian.lib.core.types import SpawnId


logger = logging.getLogger(__name__)


class SpawnFailureCategory(StrEnum):
    """Canonical lifecycle failure taxonomy for user-facing diagnostics."""

    LAUNCH_FAILURE = "launch_failure"
    HARNESS_FAILURE = "harness_failure"
    TEARDOWN_FAILURE = "teardown_failure"
    CANCELLATION = "cancellation"
    RECONCILER_ORPHAN = "reconciler_orphan"
    UNKNOWN_FAILURE = "unknown_failure"


class StartupPhase(StrEnum):
    """Canonical startup phase vocabulary per HCP spec."""

    LAUNCHING_SUBPROCESS = "launching_subprocess"
    WAITING_FOR_CONNECTION = "waiting_for_connection"
    INITIALIZING_SESSION = "initializing_session"
    HARNESS_READY = "harness.ready"
    SKILLS_LOADING = "skills.loading"
    SENDING_PROMPT = "sending_prompt"
    WAITING_FOR_RESPONSE = "waiting_for_response"
    HARNESS_FAILED = "harness.failed"


@dataclass(frozen=True)
class LifecycleEvent:
    """Lifecycle event carrying full context.

    Base fields are frozen after Phase 0 ships — mars hook compilation
    depends on this shape.
    """

    # --- Base fields (frozen) ---
    event: str
    spawn_id: str
    harness_id: str
    model: str
    agent: str | None
    ts: datetime
    seq: int
    # Event-specific fields
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class SpawnFailure:
    """Structured terminal diagnostic written alongside failed lifecycle state."""

    spawn_id: str
    ts: datetime
    exit_code: int | None
    reason: str
    category: SpawnFailureCategory = SpawnFailureCategory.UNKNOWN_FAILURE
    status: str | None = None
    origin: str | None = None
    correlation: dict[str, str] = field(default_factory=dict[str, str])
    traceback: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class StartupPhaseSignal:
    """Typed startup phase signal from adapter.

    Emitted during connection setup. Best-effort - adapters may omit
    phases they cannot observe.
    """

    phase: StartupPhase
    spawn_id: str
    ts: datetime
    seq: int
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


class StartupPhaseEmitter:
    """Typed emitter for adapter startup telemetry.

    Wraps the global observer notify path and uses the shared
    per-spawn sequence counter.
    """

    def __init__(
        self,
        spawn_id: str,
        *,
        harness_id: str = "",
        model: str | None = None,
        agent: str | None = None,
    ) -> None:
        self._spawn_id = spawn_id
        self._harness_id = harness_id
        self._model = model or ""
        self._agent = agent

    def emit(self, phase: StartupPhase, metadata: dict[str, Any] | None = None) -> None:
        """Emit a startup phase signal."""
        signal = StartupPhaseSignal(
            phase=phase,
            spawn_id=self._spawn_id,
            ts=datetime.now(UTC),
            seq=next_spawn_sequence(self._spawn_id),
            metadata=metadata or {},
        )
        event = LifecycleEvent(
            event=phase.value,
            spawn_id=self._spawn_id,
            harness_id=self._harness_id,
            model=self._model,
            agent=self._agent,
            ts=signal.ts,
            seq=signal.seq,
            payload=signal.metadata,
        )
        notify_observers(event)


class SpawnEventCounter:
    """Thread-safe monotonic counter per spawn.

    Allocated at spawn.queued time and passed to StartupPhaseEmitter
    so all event sources share one counter.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def allocate(self, spawn_id: SpawnId | str) -> int:
        """Allocate initial sequence number for a new spawn (idempotent)."""
        with self._lock:
            spawn_key = str(spawn_id)
            if spawn_key not in self._counters:
                self._counters[spawn_key] = 0
            return self._counters[spawn_key]

    def next(self, spawn_id: SpawnId | str) -> int:
        """Get next sequence number for an existing spawn."""
        with self._lock:
            spawn_key = str(spawn_id)
            if spawn_key not in self._counters:
                self._counters[spawn_key] = 0
            self._counters[spawn_key] += 1
            return self._counters[spawn_key]

    def release(self, spawn_id: SpawnId | str) -> None:
        """Clean up counter for a terminated spawn (optional)."""
        with self._lock:
            self._counters.pop(str(spawn_id), None)


_GLOBAL_EVENT_COUNTER = SpawnEventCounter()


def allocate_spawn_sequence(spawn_id: SpawnId | str) -> int:
    """Allocate the shared per-spawn sequence counter."""
    return _GLOBAL_EVENT_COUNTER.allocate(spawn_id)


def next_spawn_sequence(spawn_id: SpawnId | str) -> int:
    """Return the next shared per-spawn sequence number."""
    return _GLOBAL_EVENT_COUNTER.next(spawn_id)


def release_spawn_sequence(spawn_id: SpawnId | str) -> None:
    """Release the shared per-spawn sequence counter."""
    _GLOBAL_EVENT_COUNTER.release(spawn_id)


# Event names for core state transitions
CORE_EVENTS = frozenset(
    {
        "spawn.queued",
        "spawn.running",
        "spawn.process_exited",
        "spawn.finalizing",
        "spawn.succeeded",
        "spawn.failed",
        "spawn.cancelled",
        "spawn.archived",
    }
)

# Observability signal event names
SIGNAL_EVENTS = frozenset(
    {
        "launching_subprocess",
        "waiting_for_connection",
        "initializing_session",
        "harness.ready",
        "skills.loading",
        "sending_prompt",
        "waiting_for_response",
        "spawn.streaming",
        "spawn.updated",
        "harness.failed",
    }
)


# Compatibility re-exports — observer registry moved to lib/telemetry/.
# Keep this block after local domain definitions so observers.py can refer to
# core telemetry types without a circular import at module initialization.
from meridian.lib.telemetry.observers import (  # noqa: E402
    DebugTraceObserver,
    LifecycleObserver,
    LifecycleObserverTier,
    notify_observers,
    register_debug_trace_observer,
    register_observer,
)

__all__ = [
    "CORE_EVENTS",
    "SIGNAL_EVENTS",
    "DebugTraceObserver",
    "LifecycleEvent",
    "LifecycleObserver",
    "LifecycleObserverTier",
    "SpawnEventCounter",
    "SpawnFailure",
    "SpawnFailureCategory",
    "StartupPhase",
    "StartupPhaseEmitter",
    "StartupPhaseSignal",
    "allocate_spawn_sequence",
    "next_spawn_sequence",
    "notify_observers",
    "register_debug_trace_observer",
    "register_observer",
    "release_spawn_sequence",
]
