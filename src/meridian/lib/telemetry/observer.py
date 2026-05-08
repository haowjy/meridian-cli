"""Spawn lifecycle projection into the telemetry stream."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from meridian.lib.core.telemetry import LifecycleObserverTier, register_observer

if TYPE_CHECKING:
    from meridian.lib.core.telemetry import LifecycleEvent


# Only these sparse lifecycle events get projected into telemetry. The full
# lifecycle stream stays in spawns.jsonl.
TERMINAL_TELEMETRY_EVENTS = frozenset(
    {
        "spawn.succeeded",
        "spawn.failed",
        "spawn.cancelled",
        "spawn.process_exited",
    }
)

_REGISTER_LOCK = threading.Lock()
_registered = False


class SpawnTelemetryObserver:
    """Project sparse spawn lifecycle events into the telemetry stream.

    Registered as a diagnostic-tier lifecycle observer. Only terminal events
    and ``spawn.process_exited`` are projected; the full lifecycle stream
    remains in spawns.jsonl.
    """

    def on_event(self, event: LifecycleEvent) -> None:
        """Project approved lifecycle events to v1 telemetry."""
        if event.event not in TERMINAL_TELEMETRY_EVENTS:
            return

        # Lazy import avoids telemetry package import cycles at module load time.
        from meridian.lib.telemetry import emit_telemetry

        emit_telemetry(
            "spawn",
            event.event,
            scope="core.lifecycle",
            ids={"spawn_id": event.spawn_id},
            data=_extract_terminal_data(event),
            severity="error" if event.event == "spawn.failed" else "info",
        )


def register_spawn_telemetry_observer() -> None:
    """Register the process-wide spawn telemetry observer once."""
    global _registered
    with _REGISTER_LOCK:
        if _registered:
            return
        register_observer(SpawnTelemetryObserver(), LifecycleObserverTier.DIAGNOSTIC)
        _registered = True


def _extract_terminal_data(event: LifecycleEvent) -> dict[str, object]:
    """Extract relevant data from a sparse spawn lifecycle event."""
    data: dict[str, object] = {}
    for key in ("exit_code", "status", "duration_secs", "reason"):
        value = event.payload.get(key)
        if value is not None:
            data[key] = value
    if event.event == "spawn.failed":
        from meridian.lib.telemetry.events import make_error_data

        reason = event.payload.get("reason", "Spawn failed")
        error_data = make_error_data(message=str(reason))
        error_data["error"]["type"] = "SpawnFailed"
        data.update(error_data)
    return data


__all__ = [
    "TERMINAL_TELEMETRY_EVENTS",
    "SpawnTelemetryObserver",
    "register_spawn_telemetry_observer",
]
