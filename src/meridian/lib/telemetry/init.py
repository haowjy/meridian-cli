from __future__ import annotations

from pathlib import Path

from meridian.lib.telemetry import init_telemetry
from meridian.lib.telemetry.local_jsonl import LocalJSONLSink
from meridian.lib.telemetry.sinks import NoopSink, StderrSink, TelemetrySink


def setup_telemetry(
    *,
    runtime_root: Path | None = None,
    rootless: bool = False,
    sink: TelemetrySink | None = None,
    logical_owner: str | None = None,
) -> None:
    if sink is not None:
        init_telemetry(sink=sink, runtime_root=runtime_root)
        return
    if rootless and runtime_root is None:
        init_telemetry(sink=StderrSink())
        return
    if runtime_root is not None:
        init_telemetry(
            sink=LocalJSONLSink(runtime_root, logical_owner=logical_owner),
            runtime_root=runtime_root,
        )
        return
    init_telemetry(sink=NoopSink(), runtime_root=runtime_root)
