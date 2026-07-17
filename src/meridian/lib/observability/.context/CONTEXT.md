# lib/observability — Contracts and Tracing Notes

## DebugTracer Contract

`DebugTracer.emit()` **never raises**. On first write failure, it logs
one `WARNING` via stdlib logger, emits a telemetry event, sets
`self._disabled = True`, and returns silently on all subsequent calls.

This means trace output can silently stop mid-session. Don't use the tracer
as a correctness mechanism — it's diagnostic only.

`close()` is idempotent and thread-safe. Call it when the spawn's drain
loop ends. The file handle is opened lazily on first write (parent dirs
created automatically); no file is created for spawns that produce no
trace events.

## Output Routing

Trace output goes to the spawn artifact directory (`debug.jsonl` or similar
path configured by the caller), never to `history.jsonl` or `output.jsonl`.
This keeps spawn artifacts clean for downstream parsing.

`echo_stderr=True` mirrors every trace event to stderr — useful for local
debugging but never enabled in production spawns.

## Payload Truncation

Data values are truncated at `max_payload_bytes` (default 4096 bytes).
Truncated strings get a suffix: `"...[truncated, NB total]"`. Numeric and
boolean values pass through without truncation. `dict` and `list` are
JSON-serialized before truncation.

## `trace_helpers.py` Pattern

The four helpers (`trace_wire_recv`, `trace_wire_send`, `trace_state_change`,
`trace_parse_error`) accept `tracer: DebugTracer | None` and are no-ops
when `tracer is None`. This lets harness connection code call them
unconditionally without conditional checks at every call site.

## Relationship to Structlog

`DebugTracer` is separate from structlog. Structlog handles the
human-readable/JSON log stream (configured by `core/logging.py`).
`DebugTracer` writes machine-readable JSONL trace events to a spawn-scoped
file for post-hoc diagnosis of specific spawn failures.

Don't use `DebugTracer` for general logging — use `structlog.get_logger()`
for ops/launch/harness modules, `logging.getLogger(__name__)` for catalog/config.

## Related KB

→ KB: `$MERIDIAN_CONTEXT_KB_DIR/codebase/observability.md` (see `meridian context kb`)
