"""Telemetry public API."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.telemetry.correlation import (
    LifecycleCorrelation,
    bind_lifecycle_correlation,
    lifecycle_correlation_from_mapping,
)
from meridian.lib.telemetry.events import (
    EVENT_REGISTRY,
    TelemetryEnvelope,
    concerns_for_event,
    make_error_data,
)
from meridian.lib.telemetry.local_jsonl import LocalJSONLSink
from meridian.lib.telemetry.observers import (
    DebugTraceObserver,
    LifecycleObserver,
    LifecycleObserverTier,
    notify_observers,
    register_debug_trace_observer,
    register_observer,
)
from meridian.lib.telemetry.router import TelemetryRouter, emit_telemetry, set_global_router
from meridian.lib.telemetry.sinks import BufferingSink, NoopSink, StderrSink, TelemetrySink


def init_telemetry(*, sink: TelemetrySink | None = None, runtime_root: Path | None = None) -> None:
    """Initialize the process-wide telemetry router."""
    if sink is None:
        sink = LocalJSONLSink(runtime_root) if runtime_root is not None else NoopSink()
    set_global_router(TelemetryRouter(sink))


__all__ = [
    "EVENT_REGISTRY",
    "BufferingSink",
    "DebugTraceObserver",
    "LifecycleCorrelation",
    "LifecycleObserver",
    "LifecycleObserverTier",
    "LocalJSONLSink",
    "NoopSink",
    "StderrSink",
    "TelemetryEnvelope",
    "TelemetryRouter",
    "TelemetrySink",
    "bind_lifecycle_correlation",
    "concerns_for_event",
    "emit_telemetry",
    "init_telemetry",
    "lifecycle_correlation_from_mapping",
    "make_error_data",
    "notify_observers",
    "register_debug_trace_observer",
    "register_observer",
]
