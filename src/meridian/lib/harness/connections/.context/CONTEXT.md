# harness/connections/ — Context

## Architecture

Three concrete transports share the `HarnessConnection[SpecT]` ABC from `base.py`:

| File | Class | Transport | Protocol |
|---|---|---|---|
| `claude_ws.py` | `ClaudeConnection` | stdin/stdout NDJSON | stream-json |
| `codex_ws.py` | `CodexConnection` | WebSocket | JSON-RPC 2.0 |
| `opencode_http.py` | `OpenCodeConnection` | HTTP + SSE | REST + event stream |

**`claude_ws.py` is not a WebSocket.** The filename is a historical artifact. The connection
writes NDJSON to the subprocess stdin and reads NDJSON from stdout. The `--input-format
stream-json --output-format stream-json` flags select this mode. Do not reach for a WS
library when reading this file.

## Contracts

### HarnessConnection ABC

Required abstract methods — every subclass must implement all of these:

- `state` property → `ConnectionState` (lifecycle: `created → starting → connected → stopping → stopped / failed`)
- `harness_id`, `spawn_id`, `capabilities` properties
- `session_id` → `str | None` (None until the transport delivers a session identifier)
- `subprocess_pid` → `int | None`
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
across harnesses have different semantics. See parent `.context/` for the full terminal
event classification table.

### ConnectionCapabilities

Frozen dataclass declared as `_CAPABILITIES` on each concrete class. Set once at class
definition, not per-instance. Fields:

- `mid_turn_injection`: `"queue"` (Claude), `"interrupt_restart"` (Codex), `"http_post"` (OpenCode)
- `supports_cancel`, `supports_steer`, `runtime_model_switch`, `structured_reasoning`
- `supports_primary_observer` — only Codex and OpenCode
- `supports_runtime_hitl` — runtime HITL through the connection; only Codex
- `supported_startup_phases` — frozenset of phase names the adapter can observe

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
bidirectional NDJSON over process pipes. This avoids the need for a local HTTP server
or WebSocket listener. The `_ws.py` filename predates this transport and was kept to
avoid churn. The implementation has no WebSocket dependency.

### Codex: Port Pre-Reservation

`CodexConnection` and `passthrough/codex.py` both use `socket.socket` to bind port 0
and capture the ephemeral port before handing it to the subprocess. This is a TOCTOU
race (the OS may reassign the port before `codex app-server` binds it). If that happens,
`PortBindError` is raised and the caller retries with a new port. This is the intended
recovery path — do not suppress `PortBindError`.

### Observer Endpoint

Codex exposes a WebSocket URL (`ws://`); OpenCode exposes an HTTP URL (`http://`).
The `ObserverEndpoint` dataclass captures which transport and URL so `passthrough/` can
build the correct TUI attach command without knowing connection internals.

## Related .context/

- [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — drain loop, terminal event semantics,
  SpawnParams accounting
- [../../passthrough/.context/CONTEXT.md](../../passthrough/.context/CONTEXT.md) — uses
  `ObserverEndpoint` and `ConnectionConfig` for TUI attach sequencing
