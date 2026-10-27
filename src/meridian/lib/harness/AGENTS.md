# lib/harness/ — Harness Adapters

Mechanism side of the policy/mechanism split. Translates harness-agnostic
`SpawnParams` into a runnable process and extracts results back into Meridian's
domain types. `ops/` and `launch/` work with domain types; harness specifics stay here.

```
SpawnParams → adapter.resolve_launch_spec() → HarnessLaunchSpec
  → project_<harness>_spec_to_cli_args() → argv + env → subprocess / connection.start()
```

Two launch paths: a one-shot subprocess, and a long-lived `connections/` transport whose
events flow through the SpawnManager drain loop (streaming). Per-harness commands and
transports: [.context/adapter-notes.md](.context/adapter-notes.md).

OpenCode 1.x is legacy and frozen (registered as fallback); new OpenCode work targets
V2. `opencode_backend.py` is the version seam; transcript/storage reads pick their
dialect by DB schema, never by the installed binary or env var.

## Bootstrap Is Load-Bearing

`__init__.py:_run_bootstrap()` runs once on package import: adapters register
`HarnessBundle`s as side effects, projection modules run drift guards, then
`_enforce_spawn_params_accounting()` requires each adapter's `handled_fields` to equal
`SpawnParams` fields exactly (raises `ImportError` otherwise). Don't import individual
adapter modules before `ensure_bootstrap()` completes: the guard sees a partial set and
raises falsely. Ignoring a `SpawnParams` field is a decision; list it in
`explicitly_ignored_fields` only when the projection has no use for it. Each
`projections/project_<harness>_*.py` declares `_PROJECTED_FIELDS` and `_DELEGATED_FIELDS`;
a spec field in neither fails startup.

## Rules

- **Terminal events are harness-aware.** `event_type` is not globally unique; always
  qualify by `event.harness_id`. Streams that multiplex child work make
  `connection.primary_event_scope` part of the contract: child Codex threads / OpenCode
  task sessions reach subscribers but never complete or fail the parent. Shared
  `semantics.py` contains no harness event names; each adapter's `HarnessBundle`
  registers its own `HarnessSemantics` table.
- **Native identity is planned, not discovered.** A chat binds one immutable
  `(harness, native_store, id)`. `plan_native_identity()` picks operation and source;
  `finalize_native_identity()` pins store/ID against the final child env before argv
  projection; `observe_after_exit()` returns `PostExit` and never binds or persists;
  `launch/native_run.py` owns ordering and attribution. Source store always comes from
  the recorded chat (`SessionRequest.source_native_store`), never config hints. Cwd,
  timestamps, logs, and newest-file or prefix scans never establish chat identity.
  `legacy_native_stores.py` is a one-time import seam, not a runtime repair fallback.
- **Transcript resolution is exact.** Recorded store plus full native ID; multiple
  matches fail as `ambiguous_native_file`. Never reinterpret a recorded Claude project
  store as a config root; no ambient-store fallback.
- **Model observations have one writer**: `model_observation.observe_session_model`
  stores the raw native model token. Don't call `session_store.record_model_observation`
  elsewhere.
- **OpenCode's prompt, permissions, model and workspace grants ride
  `OPENCODE_CONFIG_CONTENT`**, built once at bind by `OpenCodeAdapter.project_launch_env()`;
  connections never rewrite it. Every surface that opens or resumes a session must select
  the reserved policy agent (`ResolvedLaunchSpec.agent_name`). See
  [.context/CONTEXT.md](.context/CONTEXT.md).
- **`env_defaults()` fills missing or blank child values** from the launch context and
  effective child env; never reread ambient env. Forced adapter policy rides
  `project_launch_env()`.
- **Spawn-owned journals** (permission, control-action, Pi runtime metadata) write through
  the published-spawn artifact mutation seam; a late write must not recreate a deleted
  spawn directory.
- **Pi usage is per assistant message**: fold each `message_end` once; `agent_end.messages`
  is repeated history.
- **No second per-spawn file store.** Harnesses expose native evidence and attempt facts;
  launch finalization owns `report.md` and `stderr.log`.
- Idle sensing: native parsing and facts stay in `*_idle.py`; `lib/idle/` owns policy.

## Entry Points

- `adapter.py` — `SpawnParams`, `HarnessAdapter`, `HarnessContract`: what an adapter must implement.
- `registry.py` — `HarnessRegistry`, `with_defaults()`.
- `claude.py` / `codex.py` / `opencode.py` / `cursor.py` / `pi.py` — adapters.
- `__init__.py` — `HARNESS_EXTENSION_TOUCHPOINTS`, `ensure_bootstrap()`.
- `semantics.py` — `HarnessSemantics` port, `normalize_event()`.
- `transcript.py`, `transcript_capture.py`, `capture_qualify.py`, `native_witness.py`,
  `opencode_snapshot.py` — read paths and capture. See
  [.context/session-transcripts.md](.context/session-transcripts.md).

Subpackages: [connections/](connections/AGENTS.md) (transports),
[projections/](projections/AGENTS.md) (spec → args/env),
[extractors/](extractors/AGENTS.md) (session ID, usage, report),
[passthrough/](passthrough/AGENTS.md) (TUI attach commands).

## Adding a Harness

Follow [`docs/harness-integration.md`](../../../../docs/harness-integration.md) (Pi is the
worked example) and touch every file in `HARNESS_EXTENSION_TOUCHPOINTS`. Adding a harness
never adds cases to shared `semantics.py`. Missing ports fail loudly at construction or
bootstrap. Checklist: [.context/CONTEXT.md](.context/CONTEXT.md).

## Depth

[.context/CONTEXT.md](.context/CONTEXT.md) — contracts, session-ID chain, Claude PTY
rationale, Codex managed-primary approval routing, OpenCode env merging, terminal event
table. [.context/adapter-notes.md](.context/adapter-notes.md) — per-harness detail.
[.context/pi-integration.md](.context/pi-integration.md) — Pi.

Related: [../launch/AGENTS.md](../launch/AGENTS.md), [../state/AGENTS.md](../state/AGENTS.md).
