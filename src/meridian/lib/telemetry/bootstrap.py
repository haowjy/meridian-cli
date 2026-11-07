from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class TelemetryMode(StrEnum):
    """Telemetry sink mode selected by a process entry seam."""

    NONE = "none"
    STDERR = "stderr"
    SEGMENT = "segment"


@dataclass(frozen=True)
class TelemetryPlan:
    """Process-level telemetry installation plan."""

    mode: TelemetryMode
    logical_owner: str = "cli"
    runtime_root: Path | None = None
    emit_usage_events: bool = True
    schedule_maintenance: bool = False


@dataclass(frozen=True)
class TelemetryHandle:
    """Opaque handle returned by install(). Allows future extension."""

    mode: TelemetryMode


def install(plan: TelemetryPlan) -> TelemetryHandle:
    """Install telemetry for the current process based on ``plan``.

    This is idempotent for callers that pass the same plan. Process entry seams
    should call it at most once.
    """

    if plan.mode == TelemetryMode.NONE:
        return TelemetryHandle(mode=plan.mode)

    from meridian.lib.telemetry import init_telemetry

    if plan.mode == TelemetryMode.STDERR:
        from meridian.lib.telemetry.sinks import StderrSink

        init_telemetry(sink=StderrSink())
        return TelemetryHandle(mode=plan.mode)

    if plan.runtime_root is None:
        return TelemetryHandle(mode=plan.mode)

    from meridian.lib.telemetry.local_jsonl import LocalJSONLSink

    sink = LocalJSONLSink(plan.runtime_root, logical_owner=plan.logical_owner)
    init_telemetry(sink=sink)
    if plan.schedule_maintenance:
        from meridian.lib.telemetry.maintenance import schedule_maintenance

        schedule_maintenance(plan.runtime_root)
    return TelemetryHandle(mode=plan.mode)
