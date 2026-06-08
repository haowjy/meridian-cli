# harness/connections/ — Bidirectional Transports

Full-duplex streaming connections between Meridian and a running harness process.
Only used by the SpawnManager drain loop and `meridian chat` — not the subprocess
path. If a spawn is one-shot (no streaming, no managed-primary), this package is
not involved.

## Mental Model

Each transport wraps a long-lived process and turns its output into a stream of
`HarnessEvent` objects consumed by the drain loop. The drain loop calls
`terminal_outcome(event)` on each event; when that returns non-None, it breaks.

Three transports exist, each different at the wire level:
- **Claude** (`claude_ws.py`): stdin/stdout NDJSON. No WebSocket despite the filename.
- **Codex** (`codex_ws.py`): real WebSocket to `codex app-server`, JSON-RPC 2.0.
  Codex's `requestApproval` and `requestUserInput` messages are dispatched to
  either `AutoAcceptHandler` (spawn paths) or `InteractiveHandler` (managed-primary attach).
- **OpenCode** (`opencode_http.py`): HTTP+SSE to `opencode serve`.

## Key Rules

**Get a connection class via `get_connection_class(harness_id, transport_id)`.**
Requires `ensure_bootstrap()` first — the registry is populated as a bootstrap side effect.

**Startup errors are classified.** `PortBindError` is retryable; other
`ConnectionStartupError` subtypes are not. The caller (SpawnManager) acts on this
distinction.

**New transport = subclass `HarnessConnection[SpecT]`**, declare `_CAPABILITIES`,
implement all abstract methods, register in the bundle. Missing registration →
`KeyError` at lookup time.

## Entry Points

- `base.py` — `HarnessConnection` ABC, `HarnessEvent`, `ConnectionCapabilities`,
  `ConnectionConfig`, `ServerRequestHandler` protocol, size constants.
- `resident_backend.py` — explicit resident-backend control seam used by
  `ResidentDrainCoordinator` for structured liveness, awaiting-done signaling,
  and follow-up turns. This seam's presence, not the harness id, selects the
  resident drain coordinator.
- `__init__.py` — `get_connection_class(harness_id, transport_id)`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — transport differences, request handler
   policy, capability flags, startup error classification.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — where connections fit in the
  translation pipeline; subprocess vs connection launch paths; terminal event semantics.
- [../passthrough/AGENTS.md](../passthrough/AGENTS.md) — uses `ConnectionConfig` for
  managed-primary TUI attach.
