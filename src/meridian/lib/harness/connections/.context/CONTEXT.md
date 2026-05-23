# harness/connections/ — Context

## Architecture

Four concrete transports share the `HarnessConnection[SpecT]` ABC from `base.py`:

| File | Class | Transport | Protocol |
|---|---|---|---|
| `claude_ws.py` | `ClaudeConnection` | stdin/stdout NDJSON | stream-json |
| `codex_ws.py` | `CodexConnection` | WebSocket | JSON-RPC 2.0 |
| `opencode_http.py` | `OpenCodeConnection` | HTTP + SSE | REST + event stream |
| `cursor_subprocess.py` | `CursorSubprocessConnection` | stdout NDJSON (read-only) | stream-json |
| `pi_rpc.py` | `PiRpcConnection` | stdin/stdout JSONL | JSON-RPC (pi rpc mode) |

**`claude_ws.py` is not a WebSocket.** The filename is a historical artifact. The connection
writes NDJSON to the subprocess stdin and reads NDJSON from stdout. The `--input-format
stream-json --output-format stream-json` flags select this mode. Do not reach for a WS
library when reading this file.

**`pi_rpc.py` (`PiRpcConnection`) has two event sources.** Primary events arrive on stdout
(JSON-RPC protocol). Lifecycle events arrive via a sidecar JSONL file (`pi_lifecycle_file.py`,
`PiLifecycleEventTailer`). The `events()` async iterator merges both sources: it reads stdout
lines and polls the sidecar file at 50ms intervals, yielding lifecycle events as they appear.
Lifecycle events on stdout are silently dropped — the sidecar is authoritative.

**Pi startup requires an initial prompt.** Unlike other harnesses that can start in a
listening state, spawned Pi RPC sessions must receive an initial user message. The
connection validates this (`_validate_initial_prompt_requirement()`) and enforces a
30-second timeout for the first response (`_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS`).

**`opencode_http.py` imports `OPENCODE_CONFIG_CONTENT_ENV` from
`meridian.lib.launch.workspace_projection`** — a cross-layer dependency on `launch/`
rather than `harness/`. This is intentional: `workspace_projection.py` was moved to
`launch/` to break a circular bootstrap dependency. Connections modules may import
shared constants from `launch/` when those constants live there by architecture
necessity.

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

- `mid_turn_injection`: `"queue"` (Claude), `"interrupt_restart"` (Codex), `"http_post"` (OpenCode),
  `"queue"` (Cursor — **but `send_user_message` always raises**; injection is not implemented
  for MVP. The type does not have an "unsupported" literal, so `"queue"` is used as a
  placeholder. Do not call `send_user_message` on a Cursor connection.)
- `supports_cancel`, `supports_steer`, `runtime_model_switch`, `structured_reasoning`
- `supports_primary_observer` — only Codex and OpenCode
- `supports_runtime_hitl` — runtime HITL through the connection; only Codex
- `supported_startup_phases` — frozenset of phase names the adapter can observe

### Pi Lifecycle Sidecar (pi_lifecycle_file.py)

`PiLifecycleEventTailer` provides incremental reading of the JSONL sidecar file
written by Pi extensions. Key design points:

- **Open/close lifecycle**: `open()` resets offset and partial buffer; `close()` releases
  the file handle. The tailer is opened in `events()` and closed in `finally`.
- **Incremental reading**: `read_ready_events()` seeks to the last-known offset and reads
  new bytes. Partial lines (no trailing newline) are buffered for the next read.
- **Catch-up at EOF**: `catch_up_to_eof()` reads all remaining events — called at
  `agent_end` and at process exit to ensure no lifecycle events are missed.
- **Parse validation**: each line goes through `parse_pi_lifecycle_event_line()` which
  validates schema version, parent spawn ID, correlation ID, and event type allowlist.
  Invalid lines become `meridian.lifecycle.parse_error` events on the stream.

The file is prepared by `prepare_pi_lifecycle_event_file()` — creates the file in the
spawn log dir and sets `PI_LIFECYCLE_EVENT_FILE_ENV` for the Pi subprocess.

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

### Pi: Dual Event Sources

`PiRpcConnection.events()` merges two async event sources: stdout lines (primary
JSON-RPC protocol stream) and the lifecycle sidecar file. The lifecycle sidecar is
polled at 50ms intervals. The merge uses `asyncio.wait_for` with a unified timeout
system — stdout reads are interleaved with sidecar polls, and timeouts trigger
sidecar re-reads. This ensures lifecycle events are delivered promptly without
starving the stdout stream.

### Pi: First-Event Timeout

A spawned Pi session must respond to the initial prompt within 30 seconds. If no
stdout event arrives in that window, the connection transitions to `failed` with
reason `pi_rpc_no_response_after_initial_prompt`. This timeout is independent of
the drain loop's child-wave timeout — it only applies during the connection startup
phase, before the Pi process is known to be responsive.

### Pi: Prompt Redaction

PiCLI args may contain secrets (e.g., `--api-key`). `redact_pi_command_for_history()`
in `pi_lifecycle_events.py` scans argv tokens for secret-bearing flags and replaces
their values with `<redacted>` before writing to the history log. This is the same
pattern as Claude's credential redaction but operates on the raw argv list rather
than the full command string.

## Related .context/

- [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — drain loop, terminal event semantics,
  SpawnParams accounting
- [../../passthrough/.context/CONTEXT.md](../../passthrough/.context/CONTEXT.md) — uses
  `ObserverEndpoint` and `ConnectionConfig` for TUI attach sequencing
- [../../../../pi_runtime/.context/CONTEXT.md](../../../../pi_runtime/.context/CONTEXT.md) — Pi
  extensions that write the lifecycle sidecar; build pipeline
- [../../../../pi_runtime/extensions/meridian-lifecycle/.context/CONTEXT.md](../../../../pi_runtime/extensions/meridian-lifecycle/.context/CONTEXT.md) —
  canonical lifecycle events consumed by the tailer
