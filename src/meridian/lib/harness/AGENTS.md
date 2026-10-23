# lib/harness/ — Harness Adapters

Mechanism side of the policy/mechanism split. Translates harness-agnostic
`SpawnParams` into a runnable process and extracts results back into Meridian's
domain types. `ops/` and `launch/` work with domain types — harness specifics stay
here.

The Meridian OpenCode adapter targets current opencode.ai CLI releases.
`opencode_backend.py` is the version seam: it resolves V1 vs V2 from
`[harness.opencode] version` (`auto`/`v1`/`v2`, default `auto`; `auto` probes
`opencode --version` and prefers V2).
Launch bind resolves the version once (`bind_opencode_spec`, the bundle's `bind_spec`
port) from the preference it projects into `MERIDIAN_HARNESS_OPENCODE_VERSION`, and
stores it on `ResolvedLaunchSpec.opencode_version`. `connections/opencode_connection.py`
is the dispatcher: one registered transport reads that version at `start()` and
delegates to the V2 session + `/api/event` transport (`opencode_v2_http.py`) or the
frozen V1 JSON + SSE transport (`opencode_http.py`), exposing `resolved_version` to
consumers such as the idle sensor. Capture and
storage/transcript reads select their OpenCode dialect by DB schema (`session_v2`
→ V2, `session` → V1) through
`detect_opencode_db_schema` in `opencode_transcript.py`, never by the installed
binary or the env var. V2's session model populates `IdleEvent.provider` for the
harness `detect_ttl` port. **OpenCode 1.x is legacy and frozen** — registered as
fallback, no investment; new work targets V2.

Claude uses the same bind port to read the settings files selected by the final child
cwd and environment. Their allows live on `ClaudeRuntimeResolution.settings_allows`,
where the permission report reads them without sending internal data through argv.

## Translation Pipeline

Every spawn goes through four steps:

```
SpawnParams                          harness-agnostic inputs
  ↓ adapter.resolve_launch_spec()
HarnessLaunchSpec                    harness-specific typed struct
  ↓ project_<harness>_spec_to_cli_args()
list[str] + env dict                 ready to exec
  ↓ subprocess / connection.start()
Running process                      events → SpawnManager drain loop
```

`adapter.py` defines `SpawnParams` — the universal input struct. Every adapter
declares `consumed_fields` (fields it uses) and `explicitly_ignored_fields`
(fields it deliberately skips). Each adapter's own `handled_fields` must equal the
`SpawnParams` fields exactly — no missing and no stale names. Another adapter
consuming a field does not cover it for yours.
`_enforce_spawn_params_accounting()` raises `ImportError` at startup on any
per-adapter drift. This is enforcement, not documentation.

Idle integration uses optional `HarnessBundle` ports: `primary_idle_sensor` for
launcher-hosted Codex/OpenCode sensing (events plus a `busy()` probe),
`native_idle_hook` for native callback observation plus unconditional
downstream delegation, and `detect_ttl` where the harness can observe cache
retention. The core passes `detect_ttl` the chat's launch-recorded
`native_store` from its session record. Adapters report the work a turn left
running as `IdleEvent.background_tasks`, but only work that wakes the session
when it ends: a report holds or delays the push, which is wrong for work that
never wakes. Claude's mod and Pi's `meridian-idle` report through
`idle arm --background-tasks`; the OpenCode 2.x sensor reports in-process (the
shells and subagents its primary's own tools started, tracked by
`opencode_running_work.py`). Codex and OpenCode 1.x report none: Codex's
background terminals and subagents do not wake the thread, and the 1.x sensor is
frozen. Core alone decides what the work means. Keep
all native parsing and facts in `*_idle.py` and its helpers; `lib/idle/` owns
policy and state. A primary observer's stream lives
until the primary closes; spawn streams may end at a reconciled terminal.

## Two Launch Paths

**Subprocess path** (`lib/launch/`): forks a one-shot process. stdout/stderr read
back on exit. Used for non-streaming spawns.

**Connection path** (`connections/`): starts a long-lived process, then connects
bidirectionally. Events flow through the SpawnManager drain loop. Used for
streaming.

Per-harness commands:
- Claude subprocess: `claude -p --output-format stream-json --verbose -`
- Claude connection: stdin/stdout NDJSON (not WebSocket despite `claude_ws.py` name)
- **Claude native-agent boundary**: Mars's launch bundle owns the native names for the
  platform's ordinary `agent`/`task` deny rules. Built-in agent types are also denied with
  scoped `--disallowedTools` entries unless
  `[harness.claude] allow_builtin_agents = true`. Generic `Agent` is retained when Mars
  effective config uses copied agents (`[settings.meridian.agent_copy] harnesses =
  ["claude"]` with `.claude` as a target). It is also retained when built-ins are allowed:
  Claude cannot deny generic `Agent` without disabling them, so that part of the platform
  rule is reported as partial. Parent/passthrough
  allowed-tool tails are merged by the permission compile; profile allowlists and
  platform denies stay authoritative.
- Codex subprocess: `codex exec --json`; connection: `codex app-server` (real WebSocket, JSON-RPC 2.0)
- **Codex constrained-launch execpolicy:** real-launch materialization atomically maintains only
  `$CODEX_HOME/rules/meridian.rules` for `read-only` and `workspace-write` launches. Its
  `meridian spawn` allow applies to every Codex session using that home. A write failure is
  reported but does not block launch.
- OpenCode subprocess: `opencode run`; connection: `opencode serve` (HTTP+SSE)
- Cursor subprocess: `cursor agent <prompt>` (stdout NDJSON, no connection path — subprocess-only)
- Pi subprocess/connection: `pi --mode rpc` (JSON-RPC stdio; Pi has no subprocess-only path — the RPC mode is the connection)

## Bootstrap Sequence Is Load-Bearing

`__init__.py:_run_bootstrap()` runs exactly once on package import. Import order
matters:
1. Adapter modules register `HarnessBundle` entries as side effects.
2. Projection modules execute import-time drift guards.
3. `_enforce_spawn_params_accounting()` validates all adapters cover all fields.

Do not import individual adapter modules before `ensure_bootstrap()` completes —
the accounting guard runs on a partial set and raises false `ImportError`.

## Key Invariants

**Spawn-owned harness journals:** permission and control-action writes must use the
published-spawn artifact mutation seam at the write point. An awaited send or dispatch
can cross spawn deletion; a late journal or cursor write must not recreate the directory.
Pi runtime metadata uses that same publication gate.

**Environment defaults differ from overrides.** `env_defaults()` receives launch
context and the effective child environment; never reread ambient env for defaults.
It fills missing or blank child values after explicit inherited/runtime
values are bound; Pi uses it for agent and session directories and to default
`PI_CACHE_RETENTION=long` only for interactive primaries, while forced adapter
policy rides `project_launch_env()`.

**OpenCode's prompt, permission policy, model and workspace grants ride
`OPENCODE_CONFIG_CONTENT`, built once at bind by `OpenCodeAdapter.project_launch_env()`;
connections never rewrite it.** Every launch names Meridian's reserved policy agent on
`ResolvedLaunchSpec.agent_name`, whether or not it has a prompt, and every surface that
opens or resumes a session must select it because `default_agent` misses resumed sessions. Detail:
[.context/CONTEXT.md](.context/CONTEXT.md#opencode-prompt-agent-and-model-delivery).

**Pi usage is per assistant message.** Fold each `message_end` increment once;
`agent_end.messages` is repeated history. Unknown usage operands leave totals
unknown. The live budget consumes the typed cumulative fold before fan-out.
Numeric cost coercion rejects boolean, nonfinite, and unrepresentable values.

**SpawnParams accounting:** every field must appear in `consumed_fields` or
`explicitly_ignored_fields` for each adapter. Adding a `SpawnParams` field
without updating all adapters → startup failure. Ignoring a field is a decision:
list it in `explicitly_ignored_fields` only when the adapter's projection has no
use for it.

**Projection drift guard:** each `projections/project_<harness>_*.py` declares
`_PROJECTED_FIELDS` and `_DELEGATED_FIELDS`. Missing a spec field from both →
startup failure.

**Native identity is planned, not discovered.** A chat binds one immutable
`(harness, native_store, id)`. `plan_native_identity()` picks the operation and
source. `SessionRequest.source_native_store` is the sole source-namespace carrier
for all harnesses; it always comes from the recorded chat, never config hints.
`finalize_native_identity()` pins store/ID against the final child env
before argv projection, so the bound key, env and argv agree. The runner binds
nonempty identities as `assigned` before exec. `observe_after_exit()` returns
`PostExit`: typed entry errors, owned entry/exit keys, and separate diagnostics.
It never binds or persists. `launch/native_run.py` owns the observation order,
entry decision, exact exit allocation and invocation attribution for every runner.
Cwd, timestamps, logs, and newest-file or prefix scans never establish tracked
chat identity.

`legacy_native_stores.py` derives one-time import candidates from recorded facts,
using these same store formats and exact validators. Store or header-contract
changes must preserve that import seam; it is not a runtime repair fallback.

**Native transcript resolution is exact.** Use the recorded store and full native
ID; multiple matching files fail as `ambiguous_native_file`, rather than selecting
a winner. `resolve_native_session_file()` takes an explicit native store;
`resolve_session_file()` accepts legacy config hints or untracked raw references.
Never reinterpret a recorded Claude project store as a config root. Claude
store derives from the final child environment without a Meridian
`CLAUDE_CONFIG_DIR` override. Finalization validates the exact native header (including dry-run); preparation
seeds `<store>/<id>.jsonl` from `<source_native_store>/<id>.jsonl`: atomic symlink
replacement within the same config root, otherwise atomic copy. Missing sources refuse
before exec, including when an ambient same-ID file exists. Model reads use the
same exact adapter resolver, with no ambient-store fallback.
`model_observation.observe_session_model` is the only writer of model observations
(primary stop, continue, and the browse detail view all call it). It stores the raw
native model token; a caller that launches with it maps it to a routable model
itself. Do not call `session_store.record_model_observation` from anywhere else: readers
need one token format, and continue used to store a launch-mapped id that a raw-id
writer beside it would have mixed in indistinguishably. OpenCode's
newly recorded store is its resolved database path, including `OPENCODE_DB`.
Claude trampoline successors travel in `PostExit` and persist in `run_boundary`,
never through the entry-ID return, chat binding, or exit
allocator. Claude's separate session-boundary plugin appends launch-correlated
SessionStart facts. A launcher-owned, PID-authorized drainer feeds them through
`NativeRun` in order, advancing the current-chat pointer without treating
observations as transcript or final-exit proof. Exact reads and resumes still
validate the native file. See
[`meridian-session-boundary`](../../claude_runtime/meridian-session-boundary/README.md).

**Terminal event classification is harness- and parent-scope-aware.** `event_type`
is NOT globally unique — always check `event.harness_id`. `turn/completed` is Codex;
OpenCode V1 uses `session.idle`, while V2 uses `session.execution.succeeded`,
`.failed`, or `.interrupted`. Some harness streams also multiplex child work on the
same connection, so `connection.primary_event_scope` is part of the contract: Codex
scopes completion to the main `threadId`; OpenCode scopes completion to the launched
parent `sessionID`. Child Codex threads and child OpenCode task sessions reach
subscribers, but do not complete/fail the parent, clear parent signals, or supply the
parent report.

## Entry Points

- `adapter.py` — `SpawnParams`, `HarnessAdapter`, and the flat `HarnessContract`.
  Source of truth for what every adapter must implement.
- `registry.py` — `HarnessRegistry`, `with_defaults()`. The global singleton.
- `claude.py` / `codex.py` / `opencode.py` / `cursor.py` — concrete adapter implementations.
  Claude's compile (`projections/permissions_claude.py`) reads
  the bind-time platform-origin tool rules so inherited grants cannot bypass the Mars
  `agent_copy` or built-in-agent boundaries.
- `__init__.py` — `HARNESS_EXTENSION_TOUCHPOINTS` and `ensure_bootstrap()`.
  Read before adding a harness — lists every file that must be touched.
- `semantics.py` — `HarnessSemantics` port and typed `EventSemantics` descriptor.
  Each adapter's `HarnessBundle` registers its own event-name descriptors and
  optional payload resolvers. `normalize_event()` dispatches by `HarnessId` before
  event name and returns raw evidence with its one normalized descriptor. A
  completion coordinator may drop duplicate events or refine their descriptors
  before delivery; refinement preserves the raw frame. For Pi, only an `agent_end`
  attempt outcome is retained privately until `agent_settled`; the frame remains
  visible to hooks and subscribers. Shared `semantics.py` contains no harness event
  names.
- `claude_idle.py` / `pi_idle.py` / `codex_idle.py` / `opencode_idle.py` — harness-owned
  cache facts, native-event parsing, live observations, and actuators registered through
  the optional idle bundle ports.
- `pi_failure.py` — Pi failure output formatting (`compact_pi_failure_output`) and
  per-event failure extraction (`pi_failure_from_payload`). Harness-owned;
  consumed by `connections/pi_rpc.py` (stderr compaction), `extractors/pi.py` (report
  extraction), and `launch/report.py` (spawn report Pi failure path).
- `common.py` — shared extraction helpers used by adapters.
- `transcript.py` — cross-harness session read path. `TranscriptMessage` (with
  `tool_call: ToolCall | None` and `is_tool_result: bool`), `ToolCall` (canonical
  harness-agnostic tool representation), and two providers
  (`JsonlTranscriptProvider`,
  `OpenCodeStorageTranscriptProvider`). Independent of the spawn/write paths — reads
  only. See [.context/session-transcripts.md](.context/session-transcripts.md) for the
  normalization table and provider selection rules.
- `transcript_capture.py` — streams and hashes native journals for sealed snapshot
  publication. Labels OpenCode captures `opencode.transcript.v1`/`.v2` by detected
  DB schema and reads through the version dispatcher. Does not own dialect completeness.
- `capture_qualify.py` — per-harness `CaptureObserver` (`observe` / `incomplete_reason`).
  New dialect = one observer + `observer_for` entry, not a patch to `NativeCapture`.
- `native_witness.py` — freshness witnesses (`FileWitness`, `OpenCodeV1Witness`,
  `OpenCodeV2Witness`) for native sources, independent of disposable projections.
- `opencode_snapshot.py` — read-only OpenCode snapshots: session witnesses, raw
  session events, and exact-turn reads over the version-dispatched DB connection.

## Subpackages

- **`connections/`** — bidirectional transport implementations.
  → [connections/AGENTS.md](connections/AGENTS.md)
- **`projections/`** — `HarnessLaunchSpec` → CLI args/env mappings.
  → [projections/AGENTS.md](projections/AGENTS.md)
- **`extractors/`** — session ID, usage, and report extraction.
  → [extractors/AGENTS.md](extractors/AGENTS.md)
- **`passthrough/`** — TUI attach commands for managed-primary sessions.
  → [passthrough/AGENTS.md](passthrough/AGENTS.md)

Interactive Claude primaries receive the bundled
`../../claude_runtime/meridian-idle/` mod and, unless
`harness.claude.spawn_watch.enabled = false`, the bundled
`../../claude_runtime/meridian-spawn-watch/` mod. Pi primaries receive the fourth
bundled extension, `../../pi_runtime/extensions/meridian-idle/`; spawned Pi RPC
sessions do not. Those runtimes translate native events into core contracts
rather than owning policy.

Primary TUI injection routes through `HarnessBundle.primary_inbox`; see the
[primary inbox contract](.context/CONTEXT.md#primary-inbox-injection).

## Adding a Harness

Full guide: [`docs/harness-integration.md`](../../../../docs/harness-integration.md) —
end-to-end with Pi as the worked example. Covers probing, adapter, projection,
extraction, connection, semantics, wrapper/runtime, session parity, model/catalog,
and verification.

Touch every file in `HARNESS_EXTENSION_TOUCHPOINTS` (`__init__.py`). The adapter's
bundle registration must include its `HarnessSemantics` table; adding a harness never
adds cases to shared `semantics.py`. Missing a port causes construction or bootstrap to
fail loudly.
See [.context/CONTEXT.md](.context/CONTEXT.md) for the full checklist.

## Anti-Patterns

**Don't invent a second per-spawn file store.** Harnesses expose native evidence and
attempt facts; launch finalization owns `report.md` and `stderr.log` in the spawn directory.

**Don't call adapter methods before `ensure_bootstrap()`** — partial registration
produces false `ImportError` from the accounting guard.

**Don't skip `consumed_fields` / `explicitly_ignored_fields` declarations** —
uncovered fields are treated as bugs, not warnings.

**Don't assume `event_type` is globally unique** — always qualify by `event.harness_id`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — full contracts, session-ID observation
   chain, Claude PTY rationale, Codex managed-primary approval routing, OpenCode env
   merging, per-harness terminal event table.
→ [.context/pi-integration.md](.context/pi-integration.md) — Pi extensions, runtime,
   and quiescence.

## Related

- `../launch/AGENTS.md` — composition seam that calls into this layer
- `../state/AGENTS.md` — authoritative spawn layout and atomic write invariants
