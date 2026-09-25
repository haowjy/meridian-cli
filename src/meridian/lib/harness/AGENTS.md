# lib/harness/ — Harness Adapters

Mechanism side of the policy/mechanism split. Translates harness-agnostic
`SpawnParams` into a runnable process and extracts results back into Meridian's
domain types. `ops/` and `launch/` work with domain types — harness specifics stay
here.

The Meridian OpenCode adapter targets current opencode.ai CLI releases.
`opencode_backend.py` is the version seam: it resolves V1 vs V2 from
`[harness.opencode] version` (`auto`/`v1`/`v2`, default `auto`; `auto` probes
`opencode --version` and prefers V2).
`connections/opencode_connection.py` is the dispatcher: one registered transport
resolves the version at `start()` and delegates to the V2 session + `/api/event`
transport (`opencode_v2_http.py`) or the frozen V1 JSON + SSE transport
(`opencode_http.py`). The launch bind seam projects the resolved preference into
`MERIDIAN_HARNESS_OPENCODE_VERSION` so connection and preview agree instead of
probing independently. Capture and storage/transcript reads select their OpenCode
dialect by DB schema (`session_v2` → V2, `session` → V1) through
`detect_opencode_db_schema` in `opencode_transcript.py`, never by the installed
binary or the env var. **OpenCode 1.x is legacy and frozen** — registered as
fallback, no investment; new work targets V2.

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
(fields it deliberately skips). Their union must cover every `SpawnParams` field;
`_enforce_spawn_params_accounting()` raises `ImportError` at startup if any field
is uncovered. This is enforcement, not documentation.

## Two Launch Paths

**Subprocess path** (`lib/launch/`): forks a one-shot process. stdout/stderr read
back on exit. Used for non-streaming spawns.

**Connection path** (`connections/`): starts a long-lived process, then connects
bidirectionally. Events flow through the SpawnManager drain loop. Used for
streaming.

Per-harness commands:
- Claude subprocess: `claude -p --output-format stream-json --verbose -`
- Claude connection: stdin/stdout NDJSON (not WebSocket despite `claude_ws.py` name)
- **Claude built-in agent denial**: Meridian injects
  `--disallowedTools Agent(Explore),Agent(Plan),Agent(General-purpose),Agent(general-purpose)`
  unconditionally so sessions use Meridian delegation instead of Claude's built-in
  subagents. Generic `Agent` is allowed only when Mars effective config has
  `[settings.meridian.agent_copy] harnesses = ["claude"]` and `.claude` is a target.
  Parent/passthrough allowed-tool tails are merged into the managed projection;
  Meridian's Agent denies stay authoritative.
- Codex subprocess: `codex exec --json`; connection: `codex app-server` (real WebSocket, JSON-RPC 2.0)
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

**SpawnParams accounting:** every field must appear in `consumed_fields` or
`explicitly_ignored_fields` for each adapter. Adding a `SpawnParams` field
without updating all adapters → startup failure.

**Projection drift guard:** each `projections/project_<harness>_*.py` declares
`_PROJECTED_FIELDS` and `_DELEGATED_FIELDS`. Missing a spec field from both →
startup failure.

**Native identity is planned, not discovered.** A chat binds one immutable
`(harness, native_store, id)`. `plan_native_identity()` picks the operation and
source. `SessionRequest.source_native_store` is the sole source-namespace carrier
for all harnesses; it always comes from the recorded chat, never config hints.
`finalize_native_identity()` pins store/ID against the final child env
before argv projection, so the bound key, env and argv agree. The runner binds
nonempty plans as `assigned` before exec; `verify_native_identity()` checks that
exact target after the attempt and never selects a replacement. Runner order:
assigned bind → initial owned `NativeEntryMismatch` check →
`verify_native_identity` → `observe_primary_session_id` diagnostics →
`finalize_run_boundary`. Typed contradictions carry expected/observed evidence;
`NativeSessionUnavailable` preserves unbound/missing/ambiguous refusal codes.
Launch refusals reach `ops/spawn/failure_policy`; neither kind permits exit
allocation or invocation attribution. Exit allocation also requires the exact native
resolver to find a valid source; unresolved exits do not fail the completed run.
`observe_session_id()` returns owned connection/process signals, then an
already-known ID, as `observed`; observations bind once and cannot overwrite.
It must not mutate adapter-instance state. Cwd, timestamps, logs, and newest-file
or prefix scans never establish tracked chat identity.

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
same exact adapter resolver, with no ambient-store fallback. OpenCode's
newly recorded store is its resolved database path, including `OPENCODE_DB`.
Claude trampoline successors travel in `PrimarySessionObservation` and persist
separately on the run, never through the entry-ID return, chat binding, or exit
allocator. Claude exit identity stays unresolved without launch-correlated evidence.

**Terminal event classification is harness- and parent-scope-aware.** `event_type`
is NOT globally unique — always check `event.harness_id`. `turn/completed` is Codex;
OpenCode uses `session.idle` for the same semantic. Some harness streams also
multiplex child work on the same connection, so `connection.primary_event_scope` is
part of the contract: Codex scopes completion to the main `threadId`; OpenCode scopes
completion to the launched parent `sessionID`. Child Codex threads and child OpenCode
task sessions stay in `history.jsonl`, but do not complete/fail the parent, clear
parent signals, or supply the parent report.

## Entry Points

- `adapter.py` — `SpawnParams`, `HarnessAdapter`, `HarnessContract` and sub-models.
  Source of truth for what every adapter must implement.
- `registry.py` — `HarnessRegistry`, `with_defaults()`. The global singleton.
- `claude.py` / `codex.py` / `opencode.py` / `cursor.py` — concrete adapter implementations.
  Claude adapter threads `ResolvedLaunchSpec.claude_native_agents_enabled` into projection so
  parent inherited `Agent` grants cannot bypass the Mars `agent_copy` boundary.
- `__init__.py` — `HARNESS_EXTENSION_TOUCHPOINTS` and `ensure_bootstrap()`.
  Read before adding a harness — lists every file that must be touched.
- `semantics.py` — `HarnessSemantics` port and typed `EventSemantics` descriptor.
  Each adapter's `HarnessBundle` registers its own event-name descriptors and
  optional payload resolvers. `normalize_event()` dispatches by `HarnessId` before
  event name and returns raw evidence with its one normalized descriptor; shared
  `semantics.py` contains no harness event names.
- `pi_failure.py` — Pi failure output formatting (`compact_pi_failure_output`) and
  history-based failure extraction (`extract_pi_failure_from_history`). Harness-owned;
  consumed by `connections/pi_rpc.py` (stderr compaction), `extractors/pi.py` (report
  extraction), and `launch/report.py` (spawn report Pi failure path).
- `common.py` — shared extraction helpers used by adapters.
- `transcript.py` — cross-harness session read path. `TranscriptMessage` (with
  `tool_call: ToolCall | None` and `is_tool_result: bool`), `ToolCall` (canonical
  harness-agnostic tool representation), and three providers
  (`JsonlTranscriptProvider`, `HistoryJsonlTranscriptProvider`,
  `OpenCodeStorageTranscriptProvider`). Independent of the spawn/write paths — reads
  only. See [.context/session-transcripts.md](.context/session-transcripts.md) for the
  normalization table and provider selection rules.
- `transcript_capture.py` — streams and hashes native journals for sealed snapshot
  publication. Labels OpenCode captures `opencode.transcript.v1`/`.v2` by detected
  DB schema and reads through the version dispatcher. Does not own dialect completeness.
- `capture_qualify.py` — per-harness `CaptureObserver` (`observe` / `incomplete_reason`).
  New dialect = one observer + `observer_for` entry, not a patch to `NativeCapture`.

## Subpackages

- **`connections/`** — bidirectional transport implementations.
  → [connections/AGENTS.md](connections/AGENTS.md)
- **`projections/`** — `HarnessLaunchSpec` → CLI args/env mappings.
  → [projections/AGENTS.md](projections/AGENTS.md)
- **`extractors/`** — session ID, usage, and report extraction.
  → [extractors/AGENTS.md](extractors/AGENTS.md)
- **`passthrough/`** — TUI attach commands for managed-primary sessions.
  → [passthrough/AGENTS.md](passthrough/AGENTS.md)

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

**Don't access artifact files directly** — use the `ArtifactStore` protocol.
Direct path access breaks tests that mock artifacts.

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
- `../state/AGENTS.md` — artifact store that extractors read from
