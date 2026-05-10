# harness/connections/

Full-duplex streaming connections between Meridian and a running harness process.
Used by the SpawnManager drain loop and `meridian chat` — not the subprocess path.

## Files

- `base.py` — `HarnessConnection` ABC, `HarnessEvent`, `ConnectionCapabilities`,
  `ConnectionConfig`, `ServerRequestHandler` protocol with `AutoAcceptHandler` /
  `InteractiveHandler`, `ObserverEndpoint`, size constants
- `claude_ws.py` — `ClaudeConnection` (stdin/stdout NDJSON, not WebSocket despite the name)
- `codex_ws.py` — `CodexConnection` (real WebSocket, JSON-RPC 2.0 over `codex app-server`)
- `opencode_http.py` — `OpenCodeConnection` (HTTP+SSE over `opencode serve`)
- `errors.py` — `ConnectionStartupError` hierarchy; `PortBindError` is retryable
- `__init__.py` — `get_connection_class(harness_id, transport_id)` lookup via bundle registry

## Entry Points

**Get a connection class:** `get_connection_class(harness_id, transport_id)` from `__init__.py`.
Requires `ensure_bootstrap()` to have run first.

**Implement a new transport:** subclass `HarnessConnection[SpecT]` in `base.py`,
declare `_CAPABILITIES`, override all abstract methods, register in the bundle.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — transport differences, request handler
  policy, capability flags, startup error classification

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — parent harness architecture; connection
  vs subprocess paths, SpawnManager drain loop, terminal event semantics
- [../passthrough/AGENTS.md](../passthrough/AGENTS.md) — uses `ConnectionConfig` and
  `HarnessConnection` for managed-primary TUI attach
