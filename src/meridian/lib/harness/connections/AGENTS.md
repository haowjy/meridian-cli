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
  `opencode_v2_http.py` V2): one registered class reads the backend version launch
  bind resolved onto the spec (`spec.opencode_version`) at `start()`, then delegates. Both transports manage `opencode serve` and share process lifecycle,
  liveness, and SSE framing; V2 overrides the `/api` session surface, basic auth,
  event envelopes, and model switch. 1.x is legacy/frozen. Connections consume the
  bound `OPENCODE_CONFIG_CONTENT` as given, including the reserved agent's permission
  rules, and select `spec.agent_name` wherever they
  create or resume a session; see
  [OpenCode prompt, agent and model delivery](../.context/CONTEXT.md#opencode-prompt-agent-and-model-delivery).
  V1 creation uses `{providerID, id}`; a fresh session's initial prompt carries `{providerID, modelID}`.
  On V1 resume the native committed model is retained and an explicit model is
  rejected with `HarnessCapabilityMismatch` (V2 applies it via `POST /api/session/{id}/model`).
  Follow-up messages omit the model to retain native state. Rejected/timed-out
  creation never retries an empty payload. V2 terminal signals are
  `session.execution.{succeeded,failed,interrupted}` (V1 uses `session.idle`/`session.error`).
  V2's `/api/event` stream is live-only: a terminal that lands between the
  pre-subscribe session GET and the SSE attach is re-polled on the liveness-timeout
  path (`_reconcile_on_stall`) through the same guarded helper, so the missed
  terminal is surfaced instead of a stall. That one-turn policy remains the spawn
  default. A stall first asks `_session_still_running()`, and a session that is still
  executing is a long tool, not a stall; the probe semantics, the busy ceiling and the
  answer that ends a stretch are in
  [OpenCode: Version Dispatcher, V2 Transport, Storage](../.context/CONTEXT.md#opencode-version-dispatcher-v2-transport-storage).
  A primary observer's stream lives until the primary closes: quiet, EOF, and
  recoverable errors reconnect without synthesizing terminal events into the
  interactive session. When the probe reports the turn ended (or cannot answer), that
  reconnect triggers the idle sensor's post-reconnect resync, which recovers a terminal
  the quiet stream missed. Spawn streams may end at a reconciled terminal.
  OpenCode's `permission.asked` (V1) / `permission.v2.asked` (V2) stream events are
  routed to the injected `ServerRequestHandler` as `HarnessRequest`s, not
  yielded raw; the handler runs in a bounded background task (a stalled reply must
  not block the SSE drain) and re-surfaces policy events through the connection's
  injected-event queue, which `events()` multiplexes ahead of the idle SSE read in
  FIFO order. Reply events (`permission.replied` / `permission.v2.replied`) are also
  observed: a reply for a still-pending request clears it and journals
  `request/resolved` (releasing the liveness key), while the stream echo of a reply
  Meridian already made passes through.
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

**Ownership-transfer guard: `reap_on_ownership_transfer_failure()`.** When external
cancellation hits during adapter startup, dispatch, or manager registration, a
child process can be stranded between owners. `reap_on_ownership_transfer_failure()`
in `base.py` catches `BaseException`, shields cleanup from repeated cancellation
deliveries in a while-not-done loop, and bounds foreground cleanup to 30 seconds.
Durable `spawn_owned` process scopes and the reaper own any residue beyond that
bound. The rejected alternative (`with suppress` single-shot) did not survive
repeated cancellation.

**The published spawn directory is a startup precondition.** Connection and process-
adoption paths never create `spawns/<id>/`. If retention deletes the aggregate across
an awaited startup boundary, artifact opens and guarded scope registration fail closed.

## Entry Points

- `base.py` — `HarnessConnection` ABC, `RawHarnessEvent`, `ConnectionCapabilities`,
  `ConnectionConfig`, `ServerRequestHandler` protocol, size constants,
  `reap_on_ownership_transfer_failure()`.
- `resident_backend.py` — explicit resident-backend control seam used by
  `ResidentDrainCoordinator` for structured liveness, awaiting-done signaling,
  and follow-up turns. This seam's presence, not the harness id, selects the
  resident drain coordinator.
- `liveness.py` — `BackendLivenessPolicy`, the shared managed-backend liveness
  classifier for Codex/OpenCode.
- `managed_backend.py` — managed backend subprocess launch helper and
  `register_spawn_owned_process()` for durable scope recording. The generic
  `register_spawn_owned_process` helper also serves stdio children (Claude, Pi,
  Cursor). Both managed launchers use `LaunchGate` to adopt the blocked child,
  persist its scope, and only then release exec; the scope handle owns the exit
  watcher and disarms the guardian after reaping. Its placement here is accepted naming debt ahead of #424's layering
  split.
- `managed_stdio.py` — spawn-lifetime stdio child ownership, durable scope
  registration, termination escalation, and bounded current-launch stderr tails.
- `pi_rpc_stream.py` — bounded byte framing and reusable temporary Pi event inbox;
  reception owns ACKs independently of the event consumer.
- `__init__.py` — `get_connection_class(harness_id, transport_id)`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — transport differences, request handler
   policy, capability flags, startup error classification.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — where connections fit in the
  translation pipeline; subprocess vs connection launch paths; terminal event semantics.
- [../passthrough/AGENTS.md](../passthrough/AGENTS.md) — uses `ConnectionConfig` for
  managed-primary TUI attach.
