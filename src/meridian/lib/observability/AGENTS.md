# lib/observability/ — Spawn-Scoped Debug Tracing

JSONL trace writer for diagnosing harness failures and wire-protocol behavior. Not metrics, not alerting, not general logging — lightweight per-spawn file output for post-hoc diagnosis.

## Mental Model

`DebugTracer` writes trace events to a spawn artifact file (`debug.jsonl`). It opens the file lazily on first write. If a write fails, it logs once, sets itself disabled, and silently no-ops all subsequent calls. Trace output can silently stop mid-session — this is by design. Never use the tracer as a correctness mechanism.

The four helper functions (`trace_wire_recv`, `trace_wire_send`, `trace_state_change`, `trace_parse_error`) accept `tracer: DebugTracer | None` and are no-ops when `tracer is None`. Harness connection code calls them unconditionally.

## Key Rules

- **`emit()` never raises.** On first write failure: one WARNING, one telemetry event, then disabled permanently.
- **`close()` is idempotent and thread-safe.** Call it when the drain loop ends.
- **Trace output goes to the spawn artifact directory — not to `history.jsonl` or `output.jsonl`.** Keep spawn artifacts clean for downstream parsing.
- **Separate from structlog.** `DebugTracer` is machine-readable JSONL to a spawn-scoped file. Structlog is the human/JSON log stream. Don't mix them.
- **`echo_stderr=True` is never used in production spawns** — it's a local debugging convenience.

## Entry Points

- `DebugTracer` — per-spawn JSONL writer
- `trace_wire_recv(tracer, event, raw_text)` — inbound wire event
- `trace_wire_send(tracer, event, payload)` — outbound wire event
- `trace_state_change(tracer, harness, from_state, to_state)` — connection state transition
- `trace_parse_error(tracer, harness, raw_text, error?)` — parse failure

## Anti-Patterns

- Don't use `DebugTracer` for general logging — use `structlog.get_logger()` (ops/launch/harness modules) or `logging.getLogger(__name__)` (catalog/config modules).
- Don't pass a tracer into non-harness code — tracing belongs in the streaming layer, not business logic.

## Related

- `../core/logging.py` — `configure_logging()` controls structlog (separate from tracer)

→ [.context/CONTEXT.md](.context/CONTEXT.md) — disable-on-failure contract, output routing, truncation limits
→ KB: `$MERIDIAN_CONTEXT_KB_DIR/codebase/observability.md` (see `meridian context kb`)
