# lib/observability/ — Spawn-Scoped Debug Tracing

`DebugTracer` writes per-spawn JSONL (`debug.jsonl` in the spawn artifact dir) for
post-hoc diagnosis of harness failures and wire behavior. Not metrics, alerting, or
general logging.

- **Never use the tracer as a correctness mechanism.** `emit()` never raises: on the first
  write failure it logs one WARNING and one telemetry event, then disables itself and
  no-ops. TUI-owned-terminal tasks may pass `report_failures=False`; it still disables.
- `close()` is idempotent and thread-safe; call it when the drain loop ends.
- **Keep trace output in its own file**, not in other spawn artifacts that downstream
  tooling parses. It is machine-readable JSONL, separate from structlog (human/JSON logs);
  don't mix them or use the tracer for general logging.
- The `trace_*` helpers accept `tracer: DebugTracer | None` and no-op on `None`; harness
  connection code calls them unconditionally. Tracing belongs in the streaming/harness
  layer, not business logic.
- `echo_stderr=True` is a local debugging convenience; never in production spawns.

[.context/CONTEXT.md](.context/CONTEXT.md) — disable-on-failure contract, output routing,
truncation limits.
