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
- **Claude native-agent boundary**: unless `[harness.claude]
  allow_builtin_agents = true`, Meridian injects scoped built-in-agent denies through
  `compile_claude_permissions()`. Generic `Agent` is gated by Mars
  `[settings.meridian.agent_copy]`: allowed when
  `harnesses = ["claude"]` and `.claude` is a target. Also retained when
  `allow_builtin_agents = true`: Claude cannot deny generic `Agent` without disabling
  built-ins, so that part of the platform rule is reported partial. Passthrough `--allowedTools` is
  merged in that compile; profile allowlists and platform denies stay authoritative. The
  `agent` platform deny also names `SendMessage` and `ListAgents`, which reach other agents
  and local Claude sessions outside Meridian.
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

### Pi UUID collision preflight

Fresh UUID minting checks readable headers in the pinned store, warns
`pi_store_unreadable_header` with the path for unreadable/invalid siblings, and
skips them. A matching readable ID or store enumeration failure still refuses.
This favors UUID4 collision improbability over blocking the whole shared store
on a torn journal; it is not an atomic ID reservation or an absolute absence
proof. Exact resume/fork source reads and post-execution ID/ancestry verification
remain fail-closed.

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

### Post-exit identity observations

`observe_after_exit(identity, entry, ...) -> PostExit` is pure observation.
Pi validates the exact assigned source/header and reads the launch-correlated
boundary sidecar. Claude reads a bounded classic-hook receipt correlated by a
fresh launch nonce and the actual Claude child PID. Its initial key must match
the planned entry; only final `SessionEnd` reasons can supply an exit key, and
the exact native transcript must validate before allocation. `clear` and
`resume` are switches, not final exits. The separate boundary plugin loads for
primary and spawned launches; the idle plugin and enabled spawn-watch plugin
remain interactive-only.
Claude's trampoline-successor evidence remains diagnostic-only and never
allocates a verified exit. Missing receipts never fall back to transcript scans.

`launch/native_run.conclude_native_run` owns ordering: attempt-facts first ID,
connection-current ID (diagnostic), adapter observation, entry verification,
exact exit publication, boundary persistence, then invocation attribution. A
verified exit is published through the same current-chat seam as live
observations, with native proof checked under its locks; `run_boundary` is
evidence only.
The facts hook observes owned live session IDs. There is no artifact-identity or
filesystem-discovery fallback.

### Primary inbox injection

`HarnessBundle.primary_inbox` is the optional mechanism port for posting to a
live primary TUI that has no `SpawnManager` connection. The CLI selects it only
for `kind == "primary"`; spawned sessions continue through their serialized
control socket. Claude resolves the latest SessionStart-published inbox socket
and record epoch for the primary's recorded worker PID, then refuses the post
unless that PID is still a live, non-zombie process born by the recorded epoch.
This prevents a stale primary row from posting to an unrelated Claude process
that reused the PID. Claude owns the native wire protocol. A post has no delivery
acknowledgement and must not be described as delivered.

### `HarnessContract` as Inspectable Surface

Every adapter declares a `HarnessContract` — a frozen Pydantic model that makes
capabilities explicit and machine-readable. Call `registry.get_contract(harness_id)`
to inspect without instantiating an adapter. The contract sub-models:

- `HarnessCapabilities` — boolean feature flags
- `ProjectionContract` — how content reaches the harness (which mode, who owns policy)
- `ExtractionContract` — what the adapter extracts from attempt and native evidence
- `ApprovalContract` — runtime HITL mode and permission routing
- `BootstrapContract` — subprocess-only vs managed-primary-attach, fork materialization
- `TransportContract` — transport IDs and whether observer/controller is required

### Terminal Event Classification — Normalize-Once Descriptors

Raw harness events arrive as open `RawHarnessEvent` envelopes (harness CLIs
are unpinned, so unknown event types are expected). `normalize_event()`
classifies each event through the per-bundle semantic port into a typed
`EventSemantics` descriptor with three fields: `activity` (turn state),
`clears_signal` (bool), and `terminal` (`TerminalEventOutcome | None`).
The result is `NormalizedHarnessEvent` carrying the raw event and its single
descriptor. Each event is normalized exactly once; downstream consumers read
the descriptor. A completion coordinator may replace that descriptor over the
same raw envelope before delivery (for example, Pi moves an `agent_end` attempt
outcome to the following `agent_settled` event); it does not hide the raw frame.

Each adapter's `HarnessBundle` registers a `HarnessSemantics` with:
- `events`: event-name to `EventSemantics` descriptor table
- `payload_resolvers`: optional callable for events needing payload inspection
- `scoped_events`: parent-scope filtering

Dispatch is by `HarnessId` (bundle lookup), then by `event_type` within that
bundle's table. Shared `semantics.py` contains no harness event names — adding
a harness never adds cases there. `event_type` is NOT globally unique; always
qualify by `event.harness_id`.

Key mappings:
- Claude: `result` event with `is_error=True` → failed; `subtype in ("", "success")` and `terminal_reason in ("", "completed")` → succeeded
- Codex: main-thread `turn/completed` resolves nested `turn.status` — `completed`/absent
  → succeeded/0, `failed` → failed/1 with upstream error, `interrupted` → cancelled/130,
  unknown → failed/1 diagnostic; `meridian/error/connectionClosed` → failed
- OpenCode V2 (2.x): parent-session `session.execution.succeeded` → succeeded;
  `session.execution.failed` → failed/1 with upstream error; an unpaired
  `session.execution.interrupted` → failed/1
- OpenCode V1 (1.x): parent-session `session.idle` → succeeded; parent-session `session.error` → failed
- Cursor: `meridian/error/connectionClosed` → failed; no explicit success event — stdout EOF
  + process exit code 0 is the success boundary (see `CursorSubprocessConnection.events()`).
- Pi: `agent_end` decodes one low-level attempt outcome but is not a session-level
  terminal or idle boundary. `agent_settled` resolves that outcome; `aborted` is
  cancellation, and missing/malformed settlement or attempt data fails closed.
  Native idle additionally requires no open compaction. `PiDrainCoordinator` then
  applies quiescence/stabilization (parent idle, no pending children/bash, no
  pending notifications).

`PrimaryEventScope` is the parent-conversation identity used for this filtering.
Connections own primary scope construction: Codex builds it from the bootstrapped
main `threadId`; OpenCode builds it from the launched parent `sessionID`.
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
| Claude | Non-zero subprocess exit at stdout EOF yields `meridian/error/connectionClosed` with the exit code and captured stderr excerpt when present, then the iterator ends. Stdout reader failure yields the same event with the read error. |
| Codex | Unexpected WebSocket reader failure or liveness timeout queues `meridian/error/connectionClosed`, then queue EOF. A clean WebSocket close queues EOF without a terminal event. Startup process exit is raised before draining and includes exit code plus the captured stderr excerpt when present. |
| OpenCode | A detected backend process exit yields `meridian/error/connectionClosed` with the exit code and captured stderr excerpt when present, then the SSE iterator ends. An SSE liveness failure yields `meridian/error/connectionClosed` naming the cause (`OpenCode event stream stalled: …`) instead of ending silently; a stream whose session is still executing is not a failure and keeps reading. |
| Cursor | Non-zero subprocess exit at stdout EOF yields `meridian/error/connectionClosed` with the exit code, then the iterator ends. Exit code zero is the success boundary and ends without a terminal event. |
| Pi | Non-zero subprocess exit yields `meridian/error/connectionClosed` with the exit code and captured stderr when present, then the iterator ends. Pi completion remains subject to its quiescence profile rather than raw EOF. |

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

OpenCode attempt facts are folded from owned live events by
`extractors/opencode.py`. V1 uses assistant message/part updates. V2 records the
assistant message ID from `session.text.ended`; finalization prefers that exact
reply from the bound DB, then live text. It never reads the ambient DB or chooses
the latest message. Transcript views independently read the chat's recorded DB
through the schema-selected native reader; an empty session is an empty view.

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

- **Built-in agents are denied by default.** Meridian injects
  `--disallowedTools Agent(Explore),Agent(Plan),Agent(General-purpose),Agent(general-purpose)`.
  `[harness.claude] allow_builtin_agents = true` removes this platform rule.

- **Generic `Agent` follows the Mars agent-copy boundary.** Meridian reads
  `mars.toml` to check whether `[settings.meridian.agent_copy] harnesses = ["claude"]` AND
  `.claude` is in `targets`. If both hold, generic `Agent` is allowed (Claude's
  native agent surface is Meridian-owned through agent copy). Otherwise, generic
  `Agent` is denied by default and delegation routes through `meridian spawn`.

The detection runs in `bind_launch_context()` through `agent_copy_enabled(project_root,
harness_id)` in `lib/catalog/agent_copy.py` (reads `mars.toml` and `mars.local.toml`; any
harness, not only Claude). The result decides whether `launch_permission_rules()` adds the
platform `agent` deny (named by Mars's `tools.capability_natives`); without agent copies it
always does, and it overrides the profile's agent rules. `compile_claude_permissions()` reads
that deny and removes generic `Agent` grants from passthrough `--allowedTools`. Claude has no spelling that disables generic `Agent` while
retaining built-in agents, so with `allow_builtin_agents = true` the compile still denies
`SendMessage`/`ListAgents` but not `Agent`, and reports that part of the platform rule
partial instead of silently disabling built-ins.

This is a Meridian platform policy, not a per-agent `tools:` field: bind adds the
platform `agent`/`task` denies (origin `platform`) and the `builtin-agents` rule to the
policy, and each harness's compile emits them. Agent profiles do not need to carry `agent: deny` entries —
the denial is inherited from the platform rule when agent copy is absent.

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

Mars's selected `harness-overrides.codex` block reaches the launch as
`execution_policy.native_config`. Bind preserves it on the launch spec; both `codex exec`
and the one-spawn `codex app-server` process receive its flattened dotted keys as `-c`
overrides. `projections/native_config.py` owns the TOML spelling, the applied/skipped
report lines, and the reserved config-key set. Meridian's model, effort, sandbox, approval,
writable roots, tool switches, multi-agent controls, and interactive `notify` remain
authoritative. Meridian writes no Codex MCP config; `mcp_servers.*` passes through as
native config. The other harness compiles report their selected native config but do not
project it.

In managed-primary mode, Meridian is the first WebSocket client (turn owner).
`requestApproval` and `requestUserInput` are routed to the turn owner by the app
server — the TUI (secondary observer) can only observe notifications.
`CodexConnection._handle_server_request()` dispatches these through an injected
request handler:
- Spawn paths: `PermissionBroker` with auto-reject (the request is answered `reject`)
- Managed-primary attach: `PermissionBroker` without auto-reject (journals and
  surfaces the request as durable events; no in-tree code replies — the control
  socket's `permission_reply` message is the only reply path)

### Codex: Live-Fork Snapshot Contract

→ [Codex live-fork snapshot](codex-live-fork.md) — snapshot, publication, and
registration contract for materialized Codex forks.

### OpenCode: Workspace Env Merging

OpenCode launch projection goes through `OPENCODE_CONFIG_CONTENT` (JSON env
override), built by `project_launch_env()` at bind. If the parent process already
exported this var, Meridian preserves unrelated settings and agents, replaces its
reserved agent entry, and appends the roots' `external_directory` grants to that
agent after the compiled policy. Suppression would silently drop workspace roots
inherited from the spawner. `OPENCODE_CONFIG_CONTENT_ENV` lives in
`projections/project_opencode_config.py` beside the serializer.

`permissions_opencode.py` owns one ordered policy compile for spawns and primaries.
`project_opencode_config.py` spells it as V1 `agent.<id>.permission` (nested maps for
scoped rules) or V2 `agents.<id>.permissions` (`{action, resource, effect}` rows).
The bind-resolved version selects the shape; connections consume the config unchanged.
OpenCode is last-match-wins, so the compile orders the wildcard default, unscoped rules,
and scoped rules by increasing literal specificity, with stricter equal matches last.
Platform denies follow profile rules, then Meridian-command grants; narrower/equal profile
restrictions are re-appended after profile-restrictable read/launch grants, non-restrictable
completion grants follow them, and workspace grants are last. The compile owns V2 aliases (`bash`→`shell`,
`write`/`patch`→`edit`, `task`→`subagent`) and resolves collisions after aliasing;
the config projector only serializes its result.

OpenCode merges same-named agent fields across config documents and appends V2 rule
arrays. The `meridian-<profile>` prefix avoids ordinary collisions; within inherited
`OPENCODE_CONFIG_CONTENT`, Meridian owns and replaces that reserved entry. A user who
deliberately defines the same reserved id in another OpenCode config document can still
participate in OpenCode's native merge, while unrelated top-level and agent settings survive.

### OpenCode: Prompt, Agent and Model Delivery

Bind projects the optional composed prompt, permission policy, a fresh session's model
and workspace-root grants into `OPENCODE_CONFIG_CONTENT`; no launch path or connection
rewrites it. `OpenCodeAdapter.project_launch_env()` is the seam (the base adapter's
default is a no-op) and `projections/project_opencode_config.py` builds the JSON with
one parse and one dump. The prompt, agent id and model travel on separate channels,
each with one owner:

- **Prompt: config, once, from bind.** V1 uses `agent.meridian-<profile>.prompt`;
  V2 uses `agents.meridian-<profile>.system`. Both point at
  `{file:<spawn-log-dir>/system-prompt.md}` and use `mode: primary`. The prompt field is
  omitted when there is no appended prompt. The id is readable in the TUI and prefixed
  to avoid ordinary user-agent collisions. The agent carries no `model`: V2 ignores it.
  Replacing the base prompt keeps AGENTS.md, OpenCode's skill listing and the
  environment block (probed on both versions). Every launch path (streaming spawn,
  managed primary, bare-TUI fallback) inherits the bound env, so none has its own
  prompt channel.
- **Agent id: decided once on `ResolvedLaunchSpec.agent_name`.**
  `resolve_launch_spec()` always sets it: the agent owns permissions and workspace
  grants even when it has no prompt. Bind always emits the agent and emits the file
  reference only when the appended prompt is non-empty. `default_agent` names it for
  sessions the user opens later in the TUI, but it only covers sessions with no agent
  of their own: a resumed V1 session replays its last message's agent and a V2 session
  keeps its stored agent (probed: resuming a session committed to `build` delivered no
  prompt). **The rule: select `spec.agent_name` wherever a session is opened or
  resumed.** A surface that cannot select it delivers nothing on resume; the one known
  case is the V2 bare TUI, which has no `--agent` (fresh sessions there take
  `default_agent`).
- **The prompt file is a launch input.** `system-prompt.md` must exist before OpenCode
  starts (a missing `{file:}` target fails the whole config), and Claude's
  `prompt_file_path` has the same dependency. `write_projection_artifacts()` writes it
  from the appended prompt once the spawn is persisted. Bind cannot: the spawn service
  binds first and publishes the spawn dir after, and `atomic_publish_dir` refuses a
  dir that already exists. `SpawnService.prepare()`, spawn execution and the primary
  runner each write it right after the spawn exists.
- **Model.** Top-level config `model` for a fresh session; a continued session keeps
  its committed model. Session create (V2) and the V1 fresh message also carry it
  explicitly, and V2 resume switches it via `POST /api/session/{id}/model` whenever
  `spec.model` is set (an exact continue drops the replayed model unless `--model` was
  explicit). The config key is the only channel the V2 bare TUI has (`--model` is
  rejected), so the subprocess projector drops `--model` for a V2 TUI and keeps it
  elsewhere (`opencode run`, V1's TUI). Effort has no working V2 channel.
- **Config writer.** `OpenCodeAdapter.project_launch_env()` is the sole producer, at
  bind. It compiles tool policy and workspace grants into the reserved config agent for
  both versions and serializes through `dump_opencode_config_content()`. Connections
  consume the bound `OPENCODE_CONFIG_CONTENT` unchanged.
- **Path limits.** OpenCode expands `{file:}` on the raw config text before parsing it,
  stopping at the first `}` and treating `"`, `\` and control characters as literal
  path text. A prompt path containing any of them fails the launch with
  `HarnessCapabilityMismatch` naming the path and characters; there is no second
  delivery channel for the edge. Non-ASCII paths work only because the config is
  serialized as raw UTF-8: a JSON `\u` escape is looked up as literal text, so a
  re-serializer that escapes non-ASCII would break the file lookup.
  Substitution is single-pass: literal `{file:..}` / `{env:..}` text inside the prompt
  arrives untouched.
- **Bare TUI needs `--standalone` on V2.** Without it the TUI is a client of the
  shared `opencode serve --service` process, which was configured by whoever
  started it: the launch env (config content, `OPENCODE_DB`) never applies.
  The flag reads the version bind resolved, the one the managed connection uses
  (including its V2 default when the probe fails), so the fallback never disagrees
  with the transport it replaces; V1 rejects `--standalone`.
- **Rejected:** a plugin `context` hook (V2-only, ships code that must load),
  session instruction entries (experimental, no black-box coverage), and repairing
  `instructions` (the V2 loader ignores it upstream).

### OpenCode: Version Dispatcher, V2 Transport, Storage

`opencode_backend.py` owns version resolution. `[harness.opencode] version`
(`auto`/`v1`/`v2`, default `auto`) is a first-class config field; `auto` probes
`opencode --version` and prefers V2, falling back to `DEFAULT_OPENCODE_VERSION`
when the binary cannot be probed. `resolve_opencode_version()` never downgrades
an explicit `v2`. The config preference is projected into the OpenCode child
env as `MERIDIAN_HARNESS_OPENCODE_VERSION` at launch bind (see
[launch context](../../launch/.context/CONTEXT.md)), and descendants inherit it.
Bind then resolves the version once, through the bundle's `bind_spec` port
(`bind_opencode_spec`), against the bound environment (its `PATH` finds the binary to
probe), and stores `v1`/`v2` on `ResolvedLaunchSpec.opencode_version`. Every reader takes
that field through `bound_opencode_version()`, which refuses a spec that skipped bind:
the permission compile and its dry-run report, the config projection, the
subprocess projector's resume-with-model guard (rejected only for V1) and bare-TUI
`--standalone`, the managed-primary preview and the connection dispatcher. One
resolution means the report can never describe a different version than the launch
emits (a report compiled for V2 while an `auto` launch emitted V1 was the bug this
replaced).

`connections/opencode_connection.py` is the single registered OpenCode transport.
At `start()` it reads the spec's bound version and delegates to `OpenCodeV2Connection`
(`opencode_v2_http.py`) or the frozen `OpenCodeV1Connection`
(`opencode_http.py`). The V2 transport subclasses V1 to reuse process lifecycle,
`BackendLivenessPolicy`, retry classification, and SSE framing, overriding only
the API surface: `/api` session routes, basic auth
(`opencode:<password>` scraped from server stdout), `POST /api/session/{id}/model`
for resume model switch, and event-envelope normalization where
`session.execution.{succeeded,failed,interrupted}` are terminal. Shared
infrastructure (`connections/base.py`, `managed_backend.py`, liveness, resident
backend) is version-independent and must not fork.

A liveness timeout is not by itself a dead stream. When `BackendLivenessPolicy`
reports `STREAM_STALLED`, the connection asks `_session_still_running()`: V1 reads
`GET /session/status` through `v1_session_running()` and V2 reads
`GET /api/session/active` through `v2_session_running()`, both in
`opencode_backend.py` (V2's is shared with the idle sensor). A session that is
still executing (V1 `busy`/`retry`, V2 present in the active listing) is a long
tool, not a stall: `record_probe()` restarts the policy's liveness window and the
connection keeps reading the same SSE response without reconnecting. Heartbeats
still do not count as activity, and the busy-stretch state lives only in the
policy, so a verdict cannot outlive the events that ended it: a real event
(`mark_activity()`) starts a new stretch. One unanswered probe inside a stretch
the server called running carries that verdict; the second in a row does not, so
an endpoint that keeps failing cannot pin a stream open. A spawned drain that
hears "still running" without one semantic event for
`_BUSY_STALL_LIMIT_SECONDS` (1800, measured from the last event) reconciles once
more and then fails with a named stall error, worded "last reported running" when
the latest answer was the unanswered miss; a primary observer has no ceiling,
because the probe reports the turn's end and the reconnect that follows resyncs
the idle sensor. Not running keeps the spawned path (one durable-outcome re-poll,
then a named stall failure) and the observer's reconnect; an unanswered probe
outside a running stretch keeps the same path.

Storage selection is by schema, never by the installed binary.
`detect_opencode_db_schema()` treats `session_v2` presence as V2 and `session` as
V1; a migrated DB keeps both, and V2 is authoritative because V2 copies every
session. `iter_opencode_db_session_events()` dispatches accordingly.
`OpenCodeV2StorageTranscriptProvider`/`interpret_opencode_v2_record()` read
`session_v2` + `session_message`, and `read_last_model()` reads
`session_v2.model` with a V1 fallback. Native capture labels sessions
`opencode.transcript.v1` or `opencode.transcript.v2` from the same detection and
streams through the dispatcher.

**OpenCode 1.x is legacy and frozen.** V1 stays registered as a fallback only;
fixing it is out of scope unless trivial. New work targets V2. Because V2 migrates
a V1 DB in place and keeps the V1 tables, a V1 *fallback* after a V2-first install
needs a separate data root (`OPENCODE_DB`/`XDG_DATA_HOME`).

### OpenCode: Background Work That Wakes the Session

The idle push is held only for work OpenCode will resume the session for, so the
V2 sensor (`opencode_idle.py`, tracker in `opencode_running_work.py`) reports a
narrower set than "everything running". Source: OpenCode v2.0.29, checked on
v2.0.32.

| Work | Wakes the primary? | Why |
|---|---|---|
| Agent `shell` tool, `background: true` | Yes | Its completion observer calls `sessions.synthetic` without overriding `resume` ([`tool/plugin/shell.ts`](https://github.com/anomalyco/opencode/blob/v2.0.29/packages/core/src/tool/plugin/shell.ts#L157-L188)). |
| `subagent` tool, `background: true` | Yes | `SubagentCompletion.deliver` adds `resume: false` only on an explicit override ([`subagent-completion.ts`](https://github.com/anomalyco/opencode/blob/v2.0.29/packages/core/src/session/subagent-completion.ts#L21-L46)). |
| `POST /api/session/:id/shell` | **No** | Completion calls `synthetic(..., resume: false)`, hard-coded, and the request body has no `resume` field ([`session.ts`](https://github.com/anomalyco/opencode/blob/v2.0.29/packages/core/src/session/session.ts#L179-L222), [protocol](https://github.com/anomalyco/opencode/blob/v2.0.29/packages/protocol/src/groups/session.ts#L480-L496)). |
| `POST /api/shell` | **No** | A standalone location-scoped shell with no session notification ([`groups/shell.ts`](https://github.com/anomalyco/opencode/blob/v2.0.29/packages/protocol/src/groups/shell.ts#L8-L37)). |
| A child session made through `POST /api/session` with `parentID` | **No** | Only the `subagent` tool attaches the completion observer; a plain child that finishes never restarts its parent. |

The two non-waking shell routes are deliberate upstream behavior, not a bug to
route around. Do not report those shells (or add a Meridian-side wake prompt) to
make them look like running work: the push would hold for work that never
resumes the session. Live canaries on v2.0.29 with the free
`opencode/deepseek-v4.1-flash-free` model agree: both agent tools wake the
parent (a new `session.execution.started` 3–5 ms after the shell exits or the
child ends, no input); `POST /api/shell` does not.

Tracking rules, all learned from the live event stream:

- Only work a tool event of the primary names counts: `session.tool.progress`
  with `metadata.shellID` or with a child `metadata.sessionID` and
  `status: "running"`. `shell.created` (command) and `session.created` with the
  primary as `parentID` only announce a candidate, so a shell or child created
  any other way, including one carrying the primary's id in its metadata, is
  never reported. Continuing an existing child (`subagent` with `sessionID` and
  `background: true`) emits no `session.created`, so it is never a candidate:
  the push may come before its wake, as it did before this tracking existed.
- Ends: `shell.exited`, `shell.deleted`, the child's own terminal
  `session.execution.*`, the primary's `session.tool.failed` naming it, or the
  primary's synthetic `session.inbox.enqueued` (`payload.metadata.source` of
  `shell` or `subagent`).
- An interrupted turn emits its terminal event **before** the aborted foreground
  shell or child ends (`shell.exited` and the child's terminal arrive 2–4 ms
  later). `shell.deleted` and the parent's `session.tool.failed` precede the
  terminal, so they must count as ends, or an interrupted foreground tool would
  hold the push forever (a certain `agent` hold never lifts without another
  turn end).
- A child waiting on a permission prompt cannot end until the user answers, so
  it does not count while the prompt is open (a hold would hide the one thing the
  user has to do). The connection consumes the raw `permission.asked` and
  `permission.replied` events, so the sensor sees the managed primary's
  `request/opened` (the ask under `params`, with the asking `sessionID`) and
  `request/resolved|failed|cancelled` (request id only). A prompt from the child
  or any descendant counts: `session.created` with a tracked `parentID` maps each
  descendant to the child it belongs to. A prompt can open before or after the
  primary's turn end; the snapshot excludes an asking child either way. Only
  permission prompts are handled; other waits on the user are not. Answering a
  prompt the tracker recorded (`request/resolved`, not `failed` or `cancelled`) is
  the user acting in the TUI without sending a message, so the sensor emits
  `user_prompt` before the re-arm: the answer closes the idle stretch and the
  re-arm opens a new one, held on the child. Without it the stretch stays open, a
  push already sent stays sent, and a later prompt or the wake turn's end never
  pushes again.
- Work that changes while the primary is idle re-arms the push: the sensor emits
  another `turn_end` (turn id `None`), which the sidecar turns into an `arm` in
  the same stretch. The new schedule replaces the old one and a stage already
  sent stays sent. A busy primary does not re-arm, because its own turn end
  reports the work. Three triggers exist: a prompt opening or closing for a
  counted child, a reconnect re-check that changed the reported work, and a
  reconnect that finds the primary's turn ended unseen. A re-arm sets
  `idle_since_ms` anew, so the cache-expiry warning and the remaining-cache
  figure count from the re-arm rather than the real turn end and drift later by
  the re-arm delay; this affects only `openai`, whose TTL is an estimate.
- `/api/event` is live-only and announces each connection with `server.connected`.
  On every reconnect (the sensor has already seen the stream; the first
  connection makes no request) it re-checks, in a background task that stops
  with the sensor, what the closed stream may have hidden, since no turn end is
  guaranteed to come:
  - The primary's own turn: `GET /api/session/active` lists the sessions whose
    turn the server is running; absent ones are inactive. If the sensor thought
    the primary busy and the server says idle, the terminal event fell in the gap:
    it corrects busy and emits the missed `turn_end`, with the work as just
    re-checked. Without it a wake turn lost in a gap leaves the sensor busy for
    good, which suppresses every re-arm. If the server runs a turn the sensor
    missed starting, busy is set so the work re-check cannot arm a push inside it.
    A snapshot the stream outdated meanwhile (it reported the primary starting or
    ending) is ignored. If the request fails, the observed state stays and one
    line is logged.
  - The work, only when something is named: `GET /api/shell` and
    `GET /api/session?parentID=` keep only ids still running, and the push
    re-arms if that changed what is reported. If the server cannot answer, it
    reports nothing and logs one line, since a hold on work that already ended
    would never lift.
  The normal path makes no request. The shell listing follows the request's
  location: `?directory=` is ignored, while the `x-opencode-directory` header and
  the `location[directory]` query both select it (the sensor uses the query, with
  the session's own `location.directory`). Starts that fell in the gap cannot be
  recovered and stay unreported; a prompt answered in the gap leaves the child
  uncounted until it ends; a whole turn (prompt and end) inside the gap leaves no
  trace to find.
- The report holds at most 16 tasks, agents first: an `agent` is the certain
  hold, so shells never push one out.
- A fresh `opencode serve` loads its provider catalog on first touch of a
  location; a prompt that races it fails with `provider.no-route`. Touch the
  location (any `GET /api/shell`) before driving a headless session.

### Cursor: Subprocess-Only, Read-Only stdout

Cursor is unmaintained. Keep it building and its existing tests passing; don't extend it
or add Cursor-specific parity work.

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

MVP scope exclusions (enforced by `_assert_supported_for_mvp()`):
`continue_fork`, `continue_session_id`, and `interactive` all raise
`HarnessCapabilityMismatch` at projection time.

### Pi: Quiescence Instead of Process Exit

Pi spawned sessions don't exit when a task completes — they stay alive to track
child spawn completion and deliver wave notifications. This means process exit
is not a valid completion signal. Instead, Meridian reads the reconciled
transitive spawn tree plus disk-backed private state (bash records and
notification markers). The drain loop delegates this policy to
`PiDrainCoordinator`, which only lets a settled success candidate finalize after
the native idle and quiescence checks pass. The shared `CompletionCoordinator`
is the single owner of completion phase and validation state; native run facts
and compaction-in-progress remain independent evidence.

Pi completes by quiescence rather than a terminal event. Resident Codex and
OpenCode also hold terminal-event completion until their persisted descendant
trees drain; plain subprocess harnesses complete on exit or a terminal event.

### Pi: Runtime Compatibility Probing

`pi_runtime_resolver.py` probes the installed `pi` binary before every launch. It
runs `pi --version` and `pi --help`, then checks the help surface for required
CLI flags. The required set differs between `primary` and `spawned` roles —
spawned requires `--mode`, `rpc`, `--no-extensions`, `-e`/`--extension`, etc. If any
required token group is missing, the launch fails with a descriptive
`PiRuntimeResolutionError`.

The compatibility probe prevents silent failures where an older Pi binary is on
PATH but lacks the flags Meridian's projection layer emits.

### Pi: Disk-Backed State vs Stdout

Pi's stdout is the JSON-RPC transport channel. Coordination state does not live there:
the extensions write disk files, and the Python side watches them. If a lifecycle-like
message appears on stdout (e.g., from a misconfigured extension), it is treated as
diagnostic noise and does not become the source of truth.

### Pi: Exact Native Identity

Managed Pi reads use the chat's recorded native store and ID. Native entry
verification and launch planning are described in [Pi integration](pi-integration.md).
Both primary and child capture snapshot the bound native key. Spawns without an
exact native source stay loose, with a reason.

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
9. `projections/permissions_<new_harness>.py`

The bundle created in step 2 must also register its per-adapter `HarnessSemantics`
table and payload resolver. Shared `semantics.py` is intentionally not an extension
touchpoint.

Missing any of these causes `ImportError` or `ValueError` at startup — the drift
guards make omissions loud.

### Anti-Patterns

**Don't invent a second per-spawn file store.** Harnesses expose native evidence and
attempt facts; launch finalization owns `report.md` and `stderr.log` in the spawn directory.

**Don't assume `event_type` is globally unique** — always check `event.harness_id`
first. `turn/completed` is a Codex event; OpenCode V1 uses `session.idle` and V2
uses `session.execution.{succeeded,failed,interrupted}` for the same semantic. Any
consumer that matches OpenCode event names itself (the idle sensor does) must
handle both dialects, selected by the connection's `resolved_version`; the shared
`OPENCODE_SEMANTICS` table covers only consumers that read it.

**Don't call adapter methods before `ensure_bootstrap()`** — the SpawnParams
accounting guard runs on a partial adapter set and will raise false `ImportError`.

**Don't skip `consumed_fields` / `explicitly_ignored_fields` declarations** — the
accounting invariant treats any uncovered field as a bug, not a warning.

## Known gaps (OpenCode V2)

Tracked, non-blocking:

- **`--fork` on the subprocess projector is unreachable.** `capabilities.supports_session_fork`
  is `False` because the routed transports (`OpenCodeV1Connection` streaming,
  `OpenCodeV2Connection`) reject `continue_fork`; launch policy downgrades a fork request
  to in-place resume with a warning. `project_opencode_subprocess.py` still projects
  `--fork` for a hypothetical routed subprocess transport — harmless but dead today.
- **Fast-failure terminals are reconciliation-sourced.** A terminal landing between the
  prompt POST and the `/api/event` attach is recovered by the one-shot stall re-poll
  (`_reconcile_on_stall`), not by a live frame; the reconcile GET is bounded by
  `_RECONCILE_TIMEOUT_SECONDS`.
- **Nested-glob specificity (low).** The broad-first order only ranks `resource == "*"`
  before scoped resources; among patterns it preserves order, so `edit(a*) allow` +
  `edit(a) deny` emits the allow last and shadows the scoped deny. Only affects policies
  mixing a wildcard-scoped allow with a narrower deny of the same action.
- **Action case normalization (low).** Action names are not case-normalized before
  alias/emission, so a mixed-case capability may not match V2's canonical action.

## Related KB

> KB lives at `$MERIDIAN_CONTEXT_KB_DIR` (see `meridian context kb`).

- `$MERIDIAN_CONTEXT_KB_DIR/codebase/harness-adapters.md` — capability matrix, per-adapter flag projection, connections subpackage, primary event scope
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/launch-system.md` — how adapters plug into the prepare/bind launch pipeline

## Related .context/

- [../../state/.context/CONTEXT.md](../../state/.context/CONTEXT.md) — authoritative spawn layout and atomic write primitives
- [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — composition seam, three driving adapters, prepare/bind split, invariants
- [../../../pi_runtime/.context/CONTEXT.md](../../../pi_runtime/.context/CONTEXT.md) — Pi TypeScript extensions, build pipeline, managed-bash / meridian-spawn-watch split
- [Pi integration](pi-integration.md) — Pi adapter, runtime, and quiescence details
- [Session transcripts](session-transcripts.md) — harness-neutral transcript normalization and providers
