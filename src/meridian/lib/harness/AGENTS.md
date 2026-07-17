# lib/harness/ — Harness Adapters

Mechanism side of the policy/mechanism split. Translates harness-agnostic
`SpawnParams` into a runnable process and extracts results back into Meridian's
domain types. `ops/` and `launch/` work with domain types — harness specifics stay
here.

The Meridian OpenCode adapter targets current opencode.ai CLI releases.

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

**SpawnParams accounting:** every field must appear in `consumed_fields` or
`explicitly_ignored_fields` for each adapter. Adding a `SpawnParams` field
without updating all adapters → startup failure.

**Projection drift guard:** each `projections/project_<harness>_*.py` declares
`_PROJECTED_FIELDS` and `_DELEGATED_FIELDS`. Missing a spec field from both →
startup failure.

**`observe_session_id()` called exactly once post-execution** (primary path only).
Priority: connection session ID → artifact extraction → known ID → filesystem scan.
Must not mutate adapter-instance state.

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
- `semantics.py` — `terminal_outcome()`, `activity_transition()`, `clears_signal()`.
  Cross-harness event classification.
- `common.py` — shared extraction helpers used by adapters.
- `transcript.py` — cross-harness session read path. `TranscriptMessage` (with
  `tool_call: ToolCall | None` and `is_tool_result: bool`), `ToolCall` (canonical
  harness-agnostic tool representation), and three providers
  (`JsonlTranscriptProvider`, `HistoryJsonlTranscriptProvider`,
  `OpenCodeStorageTranscriptProvider`). Independent of the spawn/write paths — reads
  only. See [.context/session-transcripts.md](.context/session-transcripts.md) for the
  normalization table and provider selection rules.

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

Touch every file in `HARNESS_EXTENSION_TOUCHPOINTS` (`__init__.py`). Missing any
causes `ImportError` or `ValueError` at startup — the drift guards make omissions loud.
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
