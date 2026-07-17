# lib/harness/ — Context

## Architecture

### Translation Pipeline

Every spawn goes through four steps:

```
SpawnParams                          harness-agnostic inputs
  ↓ adapter.resolve_launch_spec()
HarnessLaunchSpec                    harness-specific typed struct
  ↓ project_<harness>_spec_to_cli_args()
list[str] command + env dict         ready to exec
  ↓ subprocess / connection.start()
Running process                      events flow through SpawnManager drain loop
```

`SpawnParams` is the universal input struct (`adapter.py`). Every adapter declares
which fields it consumes (`consumed_fields`) and which it deliberately ignores
(`explicitly_ignored_fields`). Their union must cover all `SpawnParams` fields, or
`_enforce_spawn_params_accounting()` raises `ImportError` on startup.

At the harness command boundary, `bind_launch_context()` applies
`ModelSelectionContext.harness_model_id` if set — the harness-specific model string
from Mars `RunnablePath` data. This means `--model gpt-5.5 --harness opencode` passes
`openai/gpt-5.5` to the OpenCode subprocess, not bare `gpt-5.5`.

### Two Launch Paths

**Subprocess path** (`lib/launch/`): forks a one-shot process. stdout/stderr are
read back. Process exit signals completion. Used for non-streaming spawns.

**Connection path** (`lib/harness/connections/`): starts a long-lived process then
connects bidirectionally. Events flow through the `SpawnManager` drain loop. Used
for streaming execution.

Per-harness mapping:
- Claude subprocess: `claude -p --output-format stream-json --verbose -`
- Claude connection: `claude -p --input-format stream-json --output-format stream-json --verbose` (stdin/stdout NDJSON, not WebSocket despite `claude_ws.py` name)
- **Claude built-in agent denial**: Meridian injects `--disallowedTools
  Agent(Explore),Agent(Plan),Agent(General-purpose),Agent(general-purpose)`
  unconditionally. The denials merge into permission-derived `--disallowedTools` via
  `dedupe_nonempty()`. Generic
  `Agent` is gated by Mars `[settings.meridian.agent_copy]`: allowed only when
  `harnesses = ["claude"]` and `.claude` is a target. Parent/passthrough allowed-tool
  tails are merged into the managed projection; Meridian's Agent denies stay authoritative.
- Codex subprocess: `codex exec --json`; connection: `codex app-server` (real WebSocket, JSON-RPC 2.0)
- OpenCode subprocess: `opencode run`; connection: `opencode serve` (HTTP+SSE)
- Cursor subprocess: `cursor agent <prompt>` (stdout NDJSON, no connection path — subprocess-only)
- Pi subprocess/connection: `pi --mode rpc` (JSON-RPC stdio); Pi has no subprocess-only path — the RPC mode is the connection

Pi-specific extension, runtime, and quiescence details live in
[Pi integration](pi-integration.md).

### Bootstrap Sequence

`__init__.py:_run_bootstrap()` runs exactly once on package import. Import order
is load-bearing:
1. Adapter modules register `HarnessBundle` entries as side effects.
2. Projection modules execute import-time drift guards.
3. Extractor modules bind `Protocol` implementations.
4. `_enforce_spawn_params_accounting()` validates all adapters account for all fields.

Do not import individual adapter modules before `ensure_bootstrap()` completes —
the accounting guard will run on a partial registration set and fail.

## Contracts

### SpawnParams Accounting Invariant

Every `SpawnParams` field must appear in each adapter's `consumed_fields` **or**
`explicitly_ignored_fields`. Enforced at import time by `launch_spec.py:_enforce_spawn_params_accounting()`. Adding a field to `SpawnParams` without updating all adapters → `ImportError` on startup. This is not documentation — it is enforcement.

The spawn report path is no longer a `SpawnParams` field. It is derived once in
`bind_launch_context` from `resolve_spawn_log_dir(project_root, spawn_id)`; the
spawn log dir is the single authority for both `report.md` (codex `-o` target,
set on the codex `ResolvedLaunchSpec` at bind) and `system-prompt.md` (Claude's
`prompt_file_path`, set at bind).

`task_cwd` is ignored by most harness adapters because the `control_root`/`task_cwd` split
assigns different responsibilities to different layers: adapters consume `control_root`
for spawn directory resolution and `--add-dir` roots, while `task_cwd` is injected into
the agent's working context via system prompt in `bind_launch_context()` at the composition
layer. The harness command never needs the raw `task_cwd` value. **Exception:** Cursor
consumes `task_cwd` directly and projects it as `--workspace` — Cursor uses it as the
working directory for the agent subprocess, not as a system-prompt injection.

### Projection Drift Guard

Each `projections/project_<harness>_*.py` module declares `_PROJECTED_FIELDS` and
`_DELEGATED_FIELDS`. `check_projection_drift()` runs at module load. If any spec
field is missing from both sets, it raises `ImportError`. Adding a field to a
harness-specific `LaunchSpec` without updating the corresponding projection module →
startup failure.

### `observe_session_id()` Priority Chain

Called exactly once per launch by the driving adapter after the executor returns.
Must not mutate adapter-instance state. The base implementation uses a simple
fallback chain; Claude overrides it with harness-specific reconciliation:

1. `connection_session_id` — live session ID from transport layer (present for connection-based paths)
2. `extract_session_id()` — extraction from spawn artifacts (`session_id.txt`, then JSONL history)
3. `current_session_id` — previously known ID, returned as fallback
4. `detect_primary_session_id()` — filesystem scan (only when `project_root` and `started_at_epoch` provided)

Callers treat the result as authoritative. Only skip an earlier step if its source is absent (no connection, no artifacts file).

**Claude override.** `ClaudeAdapter.observe_session_id()` replaces the base
implementation's priority chain with a trampoline-aware path: after steps 1–2,
it calls `reconcile_tui_trampoline_session_id()` before falling through to
`current_session_id`. The reconciliation checks `~/.claude/history.jsonl` for
`/tui fullscreen` evidence tied to the recorded session ID, finds the next
same-project prompt with a different session ID, and verifies the successor has
a transcript whose first user message matches. If the recorded ID already has a
transcript, it is preserved — reconciliation only activates when the transcript
is missing. Fallback is always the recorded ID rather than `None`, so existing
behavior is preserved when no trampoline successor exists.

This is a Claude-specific concern. Claude's new TUI creates a transient session
when entering `/tui fullscreen`, then writes the durable transcript under a
different session ID. Meridian records the transient ID during launch; the
override repairs it to the durable ID at finalization time. Codex, OpenCode,
and Pi do not have this pattern and use the base implementation unchanged.

### `HarnessContract` as Inspectable Surface

Every adapter declares a `HarnessContract` — a frozen Pydantic model that makes
capabilities explicit and machine-readable. Call `registry.get_contract(harness_id)`
to inspect without instantiating an adapter. The contract sub-models:

- `HarnessCapabilities` — boolean feature flags
- `ProjectionContract` — how content reaches the harness (which mode, who owns policy)
- `ExtractionContract` — what the adapter extracts from artifacts
- `ApprovalContract` — runtime HITL mode and permission routing
- `BootstrapContract` — subprocess-only vs managed-primary-attach, fork materialization
- `TransportContract` — transport IDs and whether observer/controller is required

### Terminal Event Classification

`semantics.py:terminal_outcome(event)` drives the drain loop's break condition.
Returns `TerminalEventOutcome(status, exit_code, error)` or `None`.
`event_type` is NOT globally unique — always qualified by `event.harness_id`.
For harnesses whose streams can include child work, callers pass a
`PrimaryEventScope` from `HarnessConnection.primary_event_scope` into
`terminal_outcome()`, `activity_transition()`, and `clears_signal()`.

Key mappings:
- Claude: `result` event with `is_error=True` → failed; `subtype in ("", "success")` and `terminal_reason in ("", "completed")` → succeeded
- Codex: main-thread `turn/completed` → succeeded; `error/connectionClosed` → failed
- OpenCode: parent-session `session.idle` → succeeded; parent-session `session.error` → failed
- Cursor: `error/connectionClosed` → failed; no explicit success event — stdout EOF
  + process exit code 0 is the success boundary (see `CursorSubprocessConnection.events()`).
- Pi: `agent_end` → succeeded candidate; `cancelled`/`error` → failed.
  The succeeded candidate is finalized only when `PiDrainCoordinator` confirms
  quiescence (parent idle, no pending children/bash, no pending notifications).

`PrimaryEventScope` is the parent-conversation identity used for this filtering:
Codex uses the main `threadId`, and OpenCode uses the launched parent `sessionID`.
This is the single drain-loop scope contract; do not add harness-specific parallel
parameters (for example, a Codex-only thread-id compatibility argument) to semantic
helpers or coordinators.

The drain loop persists all child events before classification; scope filtering only
decides whether an event can end the parent turn, clear a pending signal, update the
parent activity state, or contribute to the parent report.

### Connection Death Shape

A connection must deliver its most diagnostic terminal evidence before iterator EOF.
If a harness has a specific terminal classification and a later generic close event,
the specific classification wins; generic transport failure must not shadow more
specific evidence already recorded.

| Harness | Unexpected death delivered to the drain |
|---|---|
| Claude | Non-zero subprocess exit at stdout EOF yields `error/connectionClosed` with the exit code in `message`, then the iterator ends. Stdout reader failure yields the same event with the read error. Stderr remains in `stderr.log`; it is not copied into the event. |
| Codex | Unexpected WebSocket reader failure or liveness timeout queues `error/connectionClosed`, then queue EOF. A clean WebSocket close queues EOF without a terminal event. Startup process exit is raised before draining and includes exit code plus the captured stderr excerpt when present. |
| OpenCode | A detected backend process exit yields `error/connectionClosed` with the exit code and captured stderr excerpt when present, then the SSE iterator ends. SSE liveness failure without a detected process exit currently ends without a terminal event. |
| Cursor | Non-zero subprocess exit at stdout EOF yields `error/connectionClosed` with the exit code, then the iterator ends. Exit code zero is the success boundary and ends without a terminal event. |
| Pi | Non-zero subprocess exit yields `error/connectionClosed` with the exit code and captured stderr when present, then the iterator ends. Pi completion remains subject to its quiescence profile rather than raw EOF. |

Tests for death classification must preserve that ordering: process exit and its code
become observable before fake iterator exhaustion. Clean EOF is a different contract.

### Inject Acknowledgment

`send_user_message()` success is a transport-level delivery claim, not proof that the
model observed or executed the message.

| Harness | What a successful return means |
|---|---|
| Claude | The NDJSON user frame was written to subprocess stdin and `drain()` completed. Claude sends no correlated acknowledgment; busy-turn acceptance remains an open runtime question. |
| Codex | The app-server returned a successful JSON-RPC result for `turn/steer` or `turn/start`. A JSON-RPC error raises instead of becoming a harness terminal event. |
| OpenCode | A session-message HTTP endpoint returned an accepted success status. This does not prove that an assistant turn was scheduled or executed. |
| Cursor | Unsupported; `send_user_message()` raises. |
| Pi | The matching prompt-command RPC response reported success. Initial prompt delivery is intentionally not acknowledgment-blocking. |

The generic control endpoint acknowledges only after the applicable connection method
returns. Do not describe that acknowledgment as semantic execution evidence.

Codex keeps a conservative unscoped fallback: if a `turn/completed` event has no
thread ID, it still counts as terminal for the parent. OpenCode does not use that
fallback once the parent session is known, because its global SSE stream can emit
unscoped-looking child task `session.idle` / `session.error` events. If no parent
scope is known at all, Meridian preserves the legacy behavior and treats OpenCode
terminal events as parent events.

OpenCode report extraction follows the same boundary and is owned by
`harness/opencode_report.py`. The OpenCode extractor delegates session-id and report
parsing there instead of duplicating event-shape logic. `extract_opencode_report()`
first resolves the parent session from `session_id.txt`, a terminal parent session
event, or the first parent user `message.updated`, then ignores child-session
assistant text while building `report.md`. Child task text remains visible through
`meridian session log`.

## Rationale

### Claude: PTY Capture for Primary Session ID

Claude's TUI emits its session ID to stdout only when stdout is a TTY. Meridian
uses `pty.openpty()` to observe this output without the harness knowing it's
captured. This is the minimum-intrusive mechanism for an otherwise unobservable
value. The legacy native-Windows branch attempts a fallback detection path
(`session_detection.py`) and is untested.

### Claude: Native Agent Routing Boundary

Claude Code ships with three built-in subagents (Explore, Plan, General-purpose) plus
a generic `Agent` tool. Meridian's policy distinguishes between these:

- **Built-in agents are always denied.** Meridian injects
  `--disallowedTools Agent(Explore),Agent(Plan),Agent(General-purpose),Agent(general-purpose)`.
  There is no config toggle — built-ins are a Meridian platform policy.

- **Generic `Agent` follows the Mars agent-copy boundary.** Meridian reads
  `mars.toml` to check whether `[settings.meridian.agent_copy] harnesses = ["claude"]` AND
  `.claude` is in `targets`. If both hold, generic `Agent` is allowed (Claude's
  native agent surface is Meridian-owned through agent copy). Otherwise, generic
  `Agent` is denied by default and delegation routes through `meridian spawn`.

The detection runs in `bind_launch_context()` via `project_has_claude_agent_copy()` in
`permissions.py`, which reads `mars.toml` and `mars.local.toml`. The result flows into
`ResolvedLaunchSpec.claude_native_agents_enabled` and is projected by
`project_claude.py`. The projection strips `Agent` and `Agent(...)` from allowed-tools
when `claude_native_agents_enabled=False`, including parent-inherited allowed-tool tails.

This is implemented as a Meridian platform policy (harness adapter injection), not as
a per-agent `tools:` field. Agent profiles do not need to carry `agent: deny` entries —
the denial is inherited from the harness adapter when agent copy is absent.

### Claude: System-Prompt File Channel

`--append-system-prompt-file <path>` avoids `ARG_MAX` limits when skills and profile
bodies are large. The adapter writes content to a temp file in the spawn log dir.
This is not an optimization — it is required for spawns with many large skills.

### Content Projection: Inline vs File Channel

Claude has a separate system-prompt channel; Codex and OpenCode do not. All three
use `"inline"` routing for references (rendered via `render_reference_blocks()`),
with `"omitted"` for empty-body files. `supports_native_file_injection=False` for
all current harnesses — `--file` flags are not used.

### Codex: Managed-Primary Approval Routing

In managed-primary mode, Meridian is the first WebSocket client (turn owner).
`requestApproval` and `requestUserInput` are routed to the turn owner by the app
server — the TUI (secondary observer) can only observe notifications.
`CodexConnection._handle_server_request()` dispatches these through an injected
request handler:
- Spawn paths: `AutoAcceptHandler` (auto-approves all)
- Managed-primary attach: `InteractiveHandler` (surfaces as durable events)

### OpenCode: Workspace Env Merging

OpenCode workspace projection goes through `OPENCODE_CONFIG_CONTENT` (JSON env
override). If the parent process already exported this var, Meridian deep-merges
new workspace entries into the parent config rather than suppressing projection.
Suppression would silently drop workspace roots inherited from the spawner.

`OPENCODE_CONFIG_CONTENT_ENV` lives in `launch/workspace_projection.py` to keep
the bootstrap dependency direction acyclic; see
[launch context](../../launch/.context/CONTEXT.md).

### Cursor: Subprocess-Only, Read-Only stdout

Cursor is a single-turn, subprocess-only harness. `cursor agent <prompt>` streams
NDJSON events to stdout then exits — there is no stdin injection, no session resume,
and no bidirectional transport. `CursorSubprocessConnection` is a connection adapter
in name only: it reads stdout until EOF and process exit, then reports done.

`task_cwd` is projected as `--workspace` rather than delegated to the system-prompt
composition layer. Cursor's agent subprocess needs the workspace path as a CLI flag
to set its working directory — system-prompt injection alone is insufficient.

`effort` appears in `_DELEGATED_FIELDS` because Mars resolves `model + effort` →
`harness_model` at bundle-build time. By the time `project_cursor_spec_to_cli_args()`
runs, `spec.model` already contains the effort-resolved model string; `effort` has
served its purpose and is not passed to the CLI.

MVP scope exclusions (enforced by `_assert_supported_for_mvp()`): per-spawn
`mcp_tools`, `continue_fork`, `continue_session_id`, and `interactive` all raise
`HarnessCapabilityMismatch` at projection time.

### Pi: Quiescence Instead of Process Exit

Pi spawned sessions don't exit when a task completes — they stay alive to track
child spawn completion and deliver wave notifications. This means process exit
is not a valid completion signal. Instead, Meridian reads the reconciled
transitive spawn tree plus disk-backed private state (bash records and
notification markers). The drain loop delegates this policy to
`PiDrainCoordinator`, which only lets an `agent_end` success candidate finalize
after the quiescence check passes.

Pi completes by quiescence rather than a terminal event. Resident Codex and
OpenCode also hold terminal-event completion until their persisted descendant
trees drain; plain subprocess harnesses complete on exit or a terminal event.

### Pi: Runtime Compatibility Probing

`pi_runtime_resolver.py` probes the installed `pi` binary before every launch. It
runs `pi --version` and `pi --help`, then checks the help surface for required
CLI flags. The required set differs between `primary` and `spawned` roles —
spawned requires `--mode`, `rpc`, `--no-extensions`, `--no-skills`, `-e`/`--extension`,
etc. If any required token group is missing, the launch fails with a descriptive
`PiRuntimeResolutionError`.

The compatibility probe prevents silent failures where an older Pi binary is on
PATH but lacks the flags Meridian's projection layer emits.

### Pi: Disk-Backed State vs Stdout

Pi's stdout is the JSON-RPC transport channel. Coordination state does not live there:
the extensions write disk files, and the Python side watches them. If a lifecycle-like
message appears on stdout (e.g., from a misconfigured extension), it is treated as
diagnostic noise and does not become the source of truth.

### Pi: Session Log Reads Spawn History

For Meridian-managed spawned Pi RPC sessions, `meridian session log <pi-spawn-id>`
reads the spawn `history.jsonl` and translates Pi `message_end` events into readable
transcript entries. It renders user prompts, assistant text, Pi tool calls/results,
and custom follow-up pings. Native Pi session-file lookup may exist as metadata, but
spawn history is the authoritative session-log source for Meridian-owned Pi spawns.

## Session Read Path

The cross-harness transcript contract is in
[session transcripts](session-transcripts.md).

## Patterns

### Adding a Harness

The full end-to-end guide is at [`docs/harness-integration.md`](../../../../../docs/harness-integration.md).
It covers probing, adapter implementation, projection, extraction, connection/semantics,
wrapper/runtime packaging, session parity, model/catalog/Mars integration, and a
verification checklist — using Pi as the worked example.

Touch every file in `HARNESS_EXTENSION_TOUCHPOINTS` (listed in `__init__.py`):
1. `core/types.py` — register `HarnessId`/`TransportId`
2. `<new_harness>.py` — adapter with `HarnessContract`, bundle registration, transport map side effect
3. `__init__.py:_run_bootstrap()` — add import wiring
4. `projections/project_<new_harness>_subprocess.py` + `_streaming.py`
5. `extractors/<new_harness>.py`
6. `registry.py:HarnessRegistry.with_defaults()`
7. `launch_spec.py:_enforce_spawn_params_accounting()` — update handled fields
8. `connections/<new_harness>_<transport>.py`
9. `projections/permission_flags.py`
10. `semantics.py:terminal_outcome()`

Missing any of these causes `ImportError` or `ValueError` at startup — the drift
guards make omissions loud.

### Anti-Patterns

**Don't access artifact files directly** — use the `ArtifactStore` protocol.
Direct path access bypasses the abstraction and breaks tests that mock artifacts.

**Don't assume `event_type` is globally unique** — always check `event.harness_id`
first. `turn/completed` is a Codex event; OpenCode uses `session.idle` for the same
semantic.

**Don't call adapter methods before `ensure_bootstrap()`** — the SpawnParams
accounting guard runs on a partial adapter set and will raise false `ImportError`.

**Don't skip `consumed_fields` / `explicitly_ignored_fields` declarations** — the
accounting invariant treats any uncovered field as a bug, not a warning.

## Related KB

> KB lives at `$MERIDIAN_CONTEXT_KB_DIR` (see `meridian context kb`).

- `$MERIDIAN_CONTEXT_KB_DIR/codebase/harness-adapters.md` — capability matrix, per-adapter flag projection, connections subpackage, primary event scope
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/launch-system.md` — how adapters plug into `build_launch_context()`

## Related .context/

- [../../state/.context/CONTEXT.md](../../state/.context/CONTEXT.md) — artifact store that `SpawnExtractor` reads from; atomic write primitives
- [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — composition seam, three driving adapters, prepare/bind split, invariants
- [../../../pi_runtime/.context/CONTEXT.md](../../../pi_runtime/.context/CONTEXT.md) — Pi TypeScript extensions, build pipeline, managed-bash / meridian-spawn-watch split
- [Pi integration](pi-integration.md) — Pi adapter, runtime, and quiescence details
- [Session transcripts](session-transcripts.md) — harness-neutral transcript normalization and providers
