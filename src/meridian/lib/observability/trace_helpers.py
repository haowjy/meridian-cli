from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meridian.lib.observability.debug_tracer import DebugTracer


def trace_state_change(
    tracer: DebugTracer | None,
    harness: str,
    from_state: str,
    to_state: str,
) -> None:
    """Emit a connection state_change event if tracer is active."""
    if tracer is None:
        return
    tracer.emit(
        "connection",
        "state_change",
        direction="internal",
        data={"from_state": from_state, "to_state": to_state, "harness": harness},
    )


def trace_wire_send(
    tracer: DebugTracer | None,
    event_name: str,
    payload: str,
    **extra: object,
) -> None:
    """Emit an outbound wire event if tracer is active."""
    if tracer is None:
        return
    data: dict[str, object] = {"payload": payload, "bytes": len(payload.encode("utf-8"))}
    data.update(extra)
    tracer.emit("wire", event_name, direction="outbound", data=data)


def trace_wire_recv(
    tracer: DebugTracer | None,
    event_name: str,
    raw_text: str,
    **extra: object,
) -> None:
    """Emit an inbound wire event if tracer is active."""
    if tracer is None:
        return
    data: dict[str, object] = {"raw_text": raw_text, "bytes": len(raw_text.encode("utf-8"))}
    data.update(extra)
    tracer.emit("wire", event_name, direction="inbound", data=data)


def trace_parse_error(
    tracer: DebugTracer | None,
    harness: str,
    raw_text: str,
    error: str | None = None,
) -> None:
    """Emit a parse_error or frame_dropped event if tracer is active."""
    if tracer is None:
        return
    data: dict[str, object] = {"raw_text": raw_text, "harness": harness}
    if error is not None:
        data["error"] = error
    tracer.emit("wire", "parse_error", direction="inbound", data=data)


__all__ = [
    "trace_parse_error",
    "trace_state_change",
    "trace_wire_recv",
    "trace_wire_send",
]
