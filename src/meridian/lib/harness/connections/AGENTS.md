# harness/connections/ — Bidirectional Transports

Full-duplex streaming connections between Meridian and a running harness process.
Only used by the SpawnManager drain loop — not the subprocess
path. If a spawn is one-shot (no streaming, no managed-primary), this package is
not involved.

## Mental Model

Each transport wraps a long-lived process and turns its output into a stream of
`RawHarnessEvent` objects consumed by the drain loop. The drain loop normalizes
each event once, applies connection-local signal state from that descriptor, and
carries the raw event plus its semantics to subscribers.

Transports differ at the wire level:
- **Claude** (`claude_ws.py`): stdin/stdout NDJSON. No WebSocket despite the filename.
- **Codex** (`codex_ws.py`): real WebSocket to a managed `codex app-server`, JSON-RPC 2.0.
  Codex's `requestApproval` and `requestUserInput` messages are dispatched to the
  injected request handler: a `PermissionBroker` that auto-rejects on spawn paths
  (`streaming/spawn_dispatch.py`), a non-rejecting one on managed-primary attach.
  `AutoAcceptHandler` is only the fallback when no handler is injected.
  Handler capabilities are declared on the `ServerRequestHandler` protocol
  (`no_runtime_hitl`) and read directly; never probe them with `getattr` defaults —
  two sites with different defaults once reported contradictory HITL support.
- **OpenCode** (`opencode_connection.py` dispatcher → `opencode_http.py` V1 /
  `opencode_v2_http.py` V2): one registered class reads `spec.opencode_version` at
  `start()` and delegates; 1.x is frozen. Connections consume the bound
  `OPENCODE_CONFIG_CONTENT` as given and select `spec.agent_name` wherever they create or
  resume a session. Permission events go to the injected `ServerRequestHandler`, never
  yielded raw. A primary observer's stream lives until the primary closes and never
  synthesizes terminal events into the interactive session. Stall/reconcile, model
  handling and the V1/V2 differences: [.context/CONTEXT.md](.context/CONTEXT.md).
- **Cursor/Pi**: narrower spawned-session transports; no resident backend seam.

## Key Rules

**Get a connection class via `get_connection_class(harness_id, transport_id)`.**
Requires `ensure_bootstrap()` first — the registry is populated as a bootstrap side effect.

**Port readiness stays local.** `PortBindError` identifies a loopback reservation
race for callers such as primary attach that can safely select another unused port.
The launch runner does not replay a harness turn after this or any other startup
failure.

**New transport = subclass `HarnessConnection[SpecT]`**, declare `_CAPABILITIES`,
implement all abstract methods, register in the bundle. Missing registration →
`KeyError` at lookup time.

**Resident control goes through `resident_backend`.** Do not add connection-level
turn-injection or managed-backend shims. Codex/OpenCode return a
`ResidentBackendControl`; callers use `begin_followup_turn()` through that seam.

**Managed backend liveness belongs to `BackendLivenessPolicy`.** Adapters feed it
activity, stall-probe answers (the busy-turn window), active turns, active requests,
backend pid, and birth time; callers consume its structured decisions instead of
inventing per-adapter alive checks.

**Wrap ownership transfers in `reap_on_ownership_transfer_failure()`** (`base.py`) so
cancellation during startup, dispatch, or manager registration cannot strand a child
between owners. It is cancellation-shielded and bounded to 30 s; a plain `suppress` did
not survive repeated cancellation.

**The published spawn directory is a startup precondition.** Connection and process-
adoption paths never create `spawns/<id>/`. If retention deletes the aggregate across
an awaited startup boundary, artifact opens and guarded scope registration fail closed.

## Entry Points

- `base.py` — `HarnessConnection` ABC, `RawHarnessEvent`, `ConnectionCapabilities`,
  `ConnectionConfig`, `ServerRequestHandler`.
- `resident_backend.py` — resident-backend control seam; its presence, not the harness id,
  selects the resident drain coordinator. `liveness.py` — `BackendLivenessPolicy`.
- `managed_backend.py` / `managed_stdio.py` — child launch via `LaunchGate`, durable
  `spawn_owned` scope registration, termination escalation. (`managed_backend.py`
  placement is accepted naming debt ahead of #424.)
- `pi_rpc_stream.py` — bounded Pi byte framing; reception owns ACKs independently of the consumer.
- `__init__.py` — `get_connection_class(harness_id, transport_id)`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — transport differences, request handler
   policy, capability flags, startup error classification.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — where connections fit in the
  translation pipeline; subprocess vs connection launch paths; terminal event semantics.
- [../passthrough/AGENTS.md](../passthrough/AGENTS.md) — uses `ConnectionConfig` for
  managed-primary TUI attach.
