# Adapter notes

Per-harness and cross-cutting detail moved out of `AGENTS.md`: look it up when touching that area. Other contracts are in [CONTEXT.md](CONTEXT.md).

## OpenCode version seam, Claude and Codex network detection

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

Claude's spec bind port reads the settings files selected by the final child cwd and
environment. Tool allows from user and project files live on
`ClaudeRuntimeResolution.settings_allows`. The adapter separately warns when the selected
user settings contain a non-empty `sandbox.network.allowedDomains` or either proxy-port
key. That report-only detection never changes the projected network or child guardrail;
narrowing settings are not modelled. Project network settings did not widen a launch-local
strict allowlist in the live probe, so they are not detection inputs.
The adapter's `shell_network_guidance()` is the sole owner of Claude's Linux-only
host-service proxy hint. `project_claude_network()` returns both its native settings and
typed effective policy; compile reads the former, while the report and child-spawn
guardrail read the latter. Shared prompt composition only places the returned guidance.

Codex's adapter warns about any `allow` under `features.network_proxy.domains` in both the
child-selected `$CODEX_HOME/config.toml` and `.codex/config.toml` in the child cwd or any
ancestor. A boolean `features.network_proxy` declares no domains. Detection is report-only:
it does not model proxy activation, project trust, or narrowing `deny` entries and never
changes the projected network or guardrail. Meridian still emits only its own inline map.
Both harness readers distinguish a missing file from an unreadable one; unreadable files
produce attributed warnings.

## Idle integration

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

## Per-harness commands and boundaries

Per-harness commands and the Claude native-agent boundary are listed in
[`CONTEXT.md`](CONTEXT.md).

- **Codex constrained-launch execpolicy:** real-launch materialization atomically maintains only
  `$CODEX_HOME/rules/meridian.rules` for spawn-capable `read-only` and `workspace-write`
  launches. Its
  `meridian spawn` allow applies to every Codex session using that home. A write failure is
  reported but does not block launch.

## Environment defaults, spawn-owned journals, Pi usage

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

**Pi usage is per assistant message.** Fold each `message_end` increment once;
`agent_end.messages` is repeated history. Unknown usage operands leave totals
unknown. The live budget consumes the typed cumulative fold before fan-out.
Numeric cost coercion rejects boolean, nonfinite, and unrepresentable values.

## Native identity and transcript resolution

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
[`meridian-session-boundary`](../../../claude_runtime/meridian-session-boundary/README.md).

An interactive Claude primary with a starting prompt (fresh, resumed or forked) receives it through
[`meridian-first-turn`](../../../claude_runtime/meridian-first-turn/README.md), not argv,
unless the mod will not run (`bind_claude_spec` decides once: mod not found, `--bare` /
`--safe-mode` or their env switches, Claude older than 2.1.314; those keep the positional prompt).
Its transcript row is `type: user` with `origin.kind: "plugin"`; Meridian's transcript
readers match on role and text, and `meridian-idle` counts a plugin origin as injected, not a
user return. If the mod is found but fails to load, the prompt is lost (the TUI shows the
error): no fallback re-adds it to argv once Meridian has dropped it. A refused or failed
submit puts the prompt in the composer with a toast; a delivered launch writes
`first-turn-delivered` beside `starting-prompt.md`, which gates resubmission after a reload.
`_MERIDIAN_CLAUDE_FIRST_TURN_PATH` is in `BLOCKED_CHILD_ENV_VARS`, so a launch never inherits it.

## Terminal event classification

**Terminal event classification is harness- and parent-scope-aware.** `event_type`
is NOT globally unique — always check `event.harness_id`. `turn/completed` is Codex;
OpenCode V1 uses `session.idle`, while V2 uses `session.execution.succeeded`,
`.failed`, or `.interrupted`. Some harness streams also multiplex child work on the
same connection, so `connection.primary_event_scope` is part of the contract: Codex
scopes completion to the main `threadId`; OpenCode scopes completion to the launched
parent `sessionID`. Child Codex threads and child OpenCode task sessions reach
subscribers, but do not complete/fail the parent, clear parent signals, or supply the
parent report.

## Entry point notes

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
  only. See [.context/session-transcripts.md](session-transcripts.md) for the
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
