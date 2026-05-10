# lib/observability/

Structured debug tracing for the streaming pipeline. Not metrics or
alerting — lightweight spawn-scoped JSONL tracing for diagnosing harness
failures and wire-protocol behavior without external tools.

## Entry Points

- `DebugTracer` — per-spawn JSONL writer; `emit()` never raises
- `trace_wire_recv(tracer, event, raw_text)` — inbound wire event helper
- `trace_wire_send(tracer, event, payload)` — outbound wire event helper
- `trace_state_change(tracer, harness, from_state, to_state)` — connection state
- `trace_parse_error(tracer, harness, raw_text, error?)` — parse failure helper

All helpers accept `tracer: DebugTracer | None` — pass `None` when tracing
is disabled; helpers are no-ops.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) for:
- `DebugTracer` disable-on-failure contract
- File vs stderr output routing
- Truncation and payload limits

## Related

- `../core/logging.py` — `configure_logging()` controls structlog (separate from tracer)
- KB: [codebase/observability.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/codebase/observability.md)
