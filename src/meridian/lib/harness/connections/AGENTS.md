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
  Codex's `requestApproval` and `requestUserInput` messages are dispatched to
  either `AutoAcceptHandler` (spawn paths) or `InteractiveHandler` (managed-primary attach).
- **OpenCode** (`opencode_connection.py` dispatcher → `opencode_http.py` V1 /
  `opencode_v2_http.py` V2): one registered class resolves the backend version at
  `start()` from `MERIDIAN_HARNESS_OPENCODE_VERSION` (projected from
  `[harness.opencode] version` at launch bind) or probes `opencode --version`, then
  delegates. Both transports manage `opencode serve` and share process lifecycle,
  liveness, and SSE framing; V2 overrides the `/api` session surface, basic auth,
  event envelopes, and model switch. 1.x is legacy/frozen. V1 creation uses
  `{providerID, id}`; a fresh session's initial prompt carries `{providerID, modelID}`.
  On V1 resume the native committed model is retained and an explicit model is
  rejected with `HarnessCapabilityMismatch` (V2 applies it via `POST /api/session/{id}/model`).
  Follow-up messages omit the model to retain native state. Rejected/timed-out
  creation never retries an empty payload. V2 terminal signals are
  `session.execution.{succeeded,failed,interrupted}` (V1 uses `session.idle`/`session.error`).
  V2's `/api/event` stream is live-only: a terminal that lands between the
  pre-subscribe session GET and the SSE attach is re-polled on the liveness-timeout
  path (`_reconcile_on_stall`, bounded by `_STALL_RECONCILE_LIMIT`) through the same
  guarded helper, so the missed terminal is surfaced instead of a stall.
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

**Startup errors are classified.** `PortBindError` is retryable; other
`ConnectionStartupError` subtypes are not. The caller (SpawnManager) acts on this
distinction.

**New transport = subclass `HarnessConnection[SpecT]`**, declare `_CAPABILITIES`,
implement all abstract methods, register in the bundle. Missing registration →
`KeyError` at lookup time.

**Resident control goes through `resident_backend`.** Do not add connection-level
turn-injection or managed-backend shims. Codex/OpenCode return a
`ResidentBackendControl`; callers use `begin_followup_turn()` through that seam.

**Managed backend liveness belongs to `BackendLivenessPolicy`.** Adapters feed it
activity, active turns, active requests, backend pid, and birth time; callers consume
its structured decisions instead of inventing per-adapter alive checks.

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
  Cursor) — its placement here is accepted naming debt ahead of #424's layering
  split.
- `managed_stdio.py` — spawn-lifetime stdio child ownership, durable scope
  registration, termination escalation, and bounded current-launch stderr tails.
- `__init__.py` — `get_connection_class(harness_id, transport_id)`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — transport differences, request handler
   policy, capability flags, startup error classification.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — where connections fit in the
  translation pipeline; subprocess vs connection launch paths; terminal event semantics.
- [../passthrough/AGENTS.md](../passthrough/AGENTS.md) — uses `ConnectionConfig` for
  managed-primary TUI attach.
