# harness/connections/ — Context

## Architecture

Five concrete transports share the `HarnessConnection[SpecT]` ABC from `base.py`:

| File | Class | Transport | Protocol |
|---|---|---|---|
| `claude_ws.py` | `ClaudeConnection` | stdin/stdout NDJSON | stream-json |
| `codex_ws.py` | `CodexConnection` | WebSocket | JSON-RPC 2.0 |
| `opencode_http.py` | `OpenCodeConnection` | HTTP + SSE | REST + event stream |
| `cursor_subprocess.py` | `CursorSubprocessConnection` | stdout NDJSON (read-only) | stream-json |
| `pi_rpc.py` | `PiRpcConnection` | stdin/stdout JSONL | JSON-RPC (Pi RPC mode) |

**`claude_ws.py` is not a WebSocket.** The filename is historical. The connection
writes NDJSON to subprocess stdin and reads NDJSON from stdout. The `--input-format
stream-json --output-format stream-json` flags select this mode.

**`pi_rpc.py` is transport-only.** It reads Pi stdout JSONL, writes prompt/abort JSON,
normalizes startup/error/session events, and emits Meridian phase diagnostics. It does
not tail a separate lifecycle event file. Pi background-work authority lives in disk files written
by Pi extensions and consumed by `lib/streaming/` (`PiDiskWatcher` / `PiQuiescenceTracker`).
If a canonical Pi lifecycle event appears on stdout, `PiRpcConnection` logs and drops it;
stdout is not the quiescence authority.

**Pi startup requires an initial prompt.** Unlike other harnesses that can start in a
listening state, spawned Pi RPC sessions must receive an initial user message. The
connection validates this (`_validate_initial_prompt_requirement()`) and enforces a
30-second timeout for the first stdout event after the prompt
(`_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS`).

**`opencode_http.py` imports `OPENCODE_CONFIG_CONTENT_ENV` from
`meridian.lib.launch.workspace_projection`** — a cross-layer dependency on `launch/`
rather than `harness/`. This is intentional: `workspace_projection.py` was moved to
`launch/` to break a circular bootstrap dependency. Connections modules may import
shared constants from `launch/` when those constants live there by architecture necessity.

## Contracts

### HarnessConnection ABC

Required abstract methods — every subclass must implement all of these:

- `state` property → `ConnectionState` (`created → starting → connected → stopping → stopped / failed`)
- `harness_id`, `spawn_id`, `capabilities` properties
- `session_id` → `str | None` (None until the transport delivers a session identifier)
- `subprocess_pid` → `int | None`
- `resident_backend` → `ResidentBackendControl | None`; Codex/OpenCode expose
  this for resident-until-done structured liveness and follow-up turns;
  non-resident transports return None. This is the only public resident control
  seam — callers do not reach for adapter-specific backend objects.
- `primary_event_scope` → `PrimaryEventScope | None`; parent-conversation identity
  for transports whose event stream can include child work. Codex returns the main
  turn `threadId`; OpenCode returns the launched parent `sessionID`. Drain and
  primary-attach callers pass this into `terminal_outcome()`,
  `activity_transition()`, and `clears_signal()` so child task events remain
  persisted but cannot finish or report for the parent.
- `scope_snapshot` → `ProcessScopeSnapshot | None`; managed backends expose the
  process-scope facts captured at launch so primary attach and cleanup paths do
  not reconstruct containment from PID fields.
- `start(config, spec)` → async; raises if already started
- `stop()` → async; idempotent
- `health()` → bool
- `send_user_message(text)` → async
- `send_cancel()` → async
- `events()` → `AsyncIterator[HarnessEvent]`

Optional methods raise `NotImplementedError` by default — only override when the transport
supports the capability:

- `start_observer(config, spec)` — observer mode; guard with `capabilities.supports_primary_observer`
- `configure_primary_runtime_requests(policy, event_sink, request_handler)`
- `inject_runtime_event(event)`
- `respond_request(request_id, decision, payload)` — Codex only
- `respond_user_input(request_id, answers)` — Codex only

### HarnessEvent

`event_type` is scoped to the producing harness — it is **not globally unique**.
Always qualify with `harness_id` before branching on `event_type`. Same-named events
across harnesses have different semantics. Terminal semantics are also parent-scope
aware when `primary_event_scope` is present. See parent `.context/` for the full
classification table.

### ConnectionCapabilities

Frozen dataclass declared as `_CAPABILITIES` on each concrete class. Set once at class
definition, not per-instance. Fields:

- `mid_turn_injection`: `"queue"` (Claude/Pi), `"interrupt_restart"` (Codex), `"http_post"` (OpenCode),
  `"queue"` (Cursor placeholder; `send_user_message` raises)
- `supports_cancel`, `supports_steer`, `runtime_model_switch`, `structured_reasoning`
- `supports_primary_observer` — only Codex and OpenCode
- `supports_runtime_hitl` — runtime HITL through the connection; only Codex
- `supported_startup_phases` — phase names the adapter can observe

### Pi RPC Stdout Path

`PiRpcConnection.events()` consumes exactly one stdout reader task. The event loop:

1. yields queued Meridian phase events (`process_spawned`, `initial_prompt_sent`, etc.);
2. waits for the first stdout event with a 30-second deadline after the initial prompt;
3. parses each stdout line as a Pi JSON object;
4. emits `first_pi_event_received` and `session_event_seen` / `session_event_absent` phase events;
5. yields normalized `HarnessEvent` objects to the streaming drain loop.

Malformed stdout becomes `meridian.lifecycle.parse_error` so bad protocol output fails
closed and stays visible. Canonical lifecycle-looking stdout events are ignored because
current Pi coordination is disk-backed.

### Pi Stop Path

`stop(reason="quiescent")` sends `{"type": "abort"}` to the Pi subprocess, waits a
5-second abort grace period, then escalates to process termination if Pi is still alive.
The streaming layer records cleanup phases (`cleanup_running`, `cleanup_completed`,
`cleanup_escalated`, `cleanup_failed`) around this transport stop.

### ServerRequestHandler — Policy Boundary

Inbound harness requests (tool approvals, user-input prompts) are dispatched through an
injected `ServerRequestHandler`:

- `AutoAcceptHandler` — auto-approves all approvals, sends empty dict for user-input.
  Used on non-interactive spawn paths. `no_runtime_hitl = True`.
- `InteractiveHandler` — converts requests to `request/opened` events on the event stream,
  expecting external code to call `respond_request()` / `respond_user_input()` later.
  Used on managed-primary attach paths. `no_runtime_hitl = False`.

The handler is injected; `HarnessConnection` implementations must not embed policy.

### Size Constants

Both are 10 MiB uniform across adapters:

- `MAX_HARNESS_MESSAGE_BYTES` — per-message cap on outbound user messages
- `MAX_INITIAL_PROMPT_BYTES` — initial prompt cap enforced by `validate_prompt_size(config)`
  before `start()` is called. Raises `PromptTooLargeError`.

### Startup Error Classification

`errors.py` defines the retry decision tree:

- `ConnectionStartupError` — base; caller must not retry
- `RetryableConnectionStartupError` — subclass; caller may retry with new config
- `PortBindError` — TOCTOU race on port reservation; retryable

## Rationale

### Claude: Stdin/Stdout Instead of WebSocket

Claude CLI supports `--input-format stream-json --output-format stream-json` for
bidirectional NDJSON over process pipes. This avoids a local HTTP server or WebSocket
listener. The `_ws.py` filename predates this transport and was kept to avoid churn.

### Codex: Port Pre-Reservation

`CodexConnection` and `passthrough/codex.py` both use `socket.socket` to bind port 0
and capture the ephemeral port before handing it to the subprocess. This is a TOCTOU
race (the OS may reassign the port before `codex app-server` binds it). If that happens,
`PortBindError` is raised and the caller retries with a new port.

### Observer Endpoint

Codex exposes a WebSocket URL (`ws://`); OpenCode exposes an HTTP URL (`http://`).
The `ObserverEndpoint` dataclass captures which transport and URL so `passthrough/` can
build the correct TUI attach command without knowing connection internals.

### Pi: JSON-RPC Transport vs Quiescence State

Pi's connection layer should stay a protocol adapter. It knows how to launch Pi RPC,
send prompts/abort, read stdout JSONL, redact command history, and classify startup
failure. It should not grow child-spawn or background-task policy. That policy belongs
in `lib/streaming/` because it combines harness events with spawn records, bash records,
notification markers, deadlines, and cleanup decisions.

### Pi: First-Event Timeout

A spawned Pi session must respond to the initial prompt within 30 seconds. If no stdout
event arrives in that window, the connection transitions to `failed` with reason
`pi_rpc_no_response_after_initial_prompt`. This timeout is independent of the drain
loop's child-wave timeout; it only applies before Pi is known responsive.

### Pi: Prompt Redaction

Pi CLI args may contain secrets (e.g., `--api-key`). `redact_pi_command_for_history()`
in `pi_lifecycle_events.py` scans argv tokens for secret-bearing flags and replaces
their values with `<redacted>` before writing to history.

## Related .context/

- [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — drain loop, terminal event semantics,
  `SpawnParams` accounting
- [../../passthrough/.context/CONTEXT.md](../../passthrough/.context/CONTEXT.md) — uses
  `ObserverEndpoint` and `ConnectionConfig` for TUI attach sequencing
- [../../../../pi_runtime/.context/CONTEXT.md](../../../../pi_runtime/.context/CONTEXT.md) — Pi
  extensions that write disk-backed coordination state
- [../../../streaming/.context/CONTEXT.md](../../../streaming/.context/CONTEXT.md) — Pi
  quiescence drain policy that consumes disk state
