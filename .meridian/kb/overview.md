# Meridian — System Overview

Meridian is a harness-agnostic multi-agent coordination CLI. It launches, tracks, and supervises AI agent processes (spawns) across multiple harness backends. It is not an execution engine or data warehouse — it is a coordination layer.

## Major Subsystems

```
src/meridian/
  cli/            CLI entry point (cyclopts), command groups, registration
  server/         MCP stdio server (FastMCP)
  lib/
    ops/          Operation manifest — shared API surface for CLI and MCP
    harness/      Subprocess adapters: Claude, Codex, OpenCode, Direct
    state/        JSONL event stores (spawns, sessions), mutable JSON (work items)
    catalog/      Model resolution, agent/skill loading, default agent policy
    launch/       Spawn lifecycle: prepare → resolve → process → finalize
    config/       Settings model, precedence chain, TOML read/write, workspace config
    context/      Context path resolver: work/kb backend config (local vs git-backed)
    hooks/        Hook dispatch system: lifecycle events → builtins or shell commands
    core/         Shared primitives: type aliases, JSONL codec, signal handling, output sink
    platform/     Cross-platform primitives: file locking, process tree termination, OS detection
  plugin_api/     Stable plugin API v1: Hook/HookContext types, state/git/config/fs helpers
    app/          REST server exposing spawn management as HTTP API (SPEC_ONLY launch path)
    streaming/    Connection abstractions for harness subprocess output; streaming runner
    observability/ Structured logging context, debug tracing, spawn-scoped log binding
    safety/       Env sanitization, security constraints
    sync/         Idempotent sync operations, mars sync integration
    adapters/     Harness adapter registration (see harness/)
    utils/        Additional utilities beyond core/
  dev/            Token-efficient pytest wrapper
```

## How Subsystems Connect

**Entry points → ops → lib:**
- `meridian spawn ...` (CLI) and `spawn_create` (MCP tool) both call `spawn_create_sync` from `ops/spawn/api.py`
- All user-facing operations are declared in `ops/commands.py` as `OperationSpec` instances
- CLI commands are auto-generated or explicitly registered from the manifest; the MCP server registers the same ops as FastMCP tools
- See `ops/overview.md` for the manifest architecture

**Launch flow:**

Composition is centralized in `lib/launch/context.py:build_launch_context()`. Every launch path builds a `SpawnRequest` (caller intent DTO) and `LaunchRuntime` (surface/env/paths), calls the factory, then executes or observes. Driving adapters do not compose directly — see `launch/overview.md` for invariant details.

1. `ops/spawn/prepare.py` — validates input, resolves model aliases (via catalog), builds `SpawnRequest` with `SPAWN_PREPARE` surface, calls `build_launch_context(dry_run=True)` for prompt composition and preview argv
2. `lib/launch/context.py:build_launch_context()` — sole composition seam: resolves policies (via `policies.py:resolve_policies()`), permission pipeline, prompt, argv, child env — returns `LaunchContext`
3. Dispatch splits by launch surface:
   - **Primary (CLI) path:** `lib/launch/__init__.py:launch_primary()` builds `SpawnRequest`+`LaunchRuntime`, calls factory for preview, then delegates to `lib/launch/process/:run_harness_process()`. Inside `run_harness_process()`: creates session, registers spawn as queued (recording runner_pid), materializes fork if needed (after row exists), rebuilds `LaunchContext` with real paths, runs PTY/pipe subprocess, finalizes inline (no `enrich_finalize`), calls `observe_session_id()` once post-execution
   - **Spawn/subagent path:** `ops/spawn/execute.py` builds `SpawnRequest`+`LaunchRuntime(SPEC_ONLY)`, calls factory after spawn row and session exist, then calls `lib/launch/streaming_runner.py:execute_with_streaming()` — `process/` package is not involved
   - **REST app path:** `lib/app/server.py` builds `SpawnRequest`+`LaunchRuntime(SPEC_ONLY)`, calls factory, uses `launch_ctx.spec` to start streaming connections via `SpawnManager`; finalization is background-async
   - **CLI streaming-serve path:** `cli/streaming_serve.py` builds `SpawnRequest`+`LaunchRuntime(SPEC_ONLY)`, calls factory, then calls `run_streaming_spawn()` from `streaming_runner.py` directly; finalizes inline under `signal_coordinator().mask_sigterm()`
4. `lib/launch/streaming_runner.py:execute_with_streaming()` — async subprocess executor for spawn/subagent path: stdout/stderr capture, report watchdog, stdin feeding, exit code mapping, heartbeat task, `mark_finalizing` CAS, writes exited event after process exits
5. `lib/launch/extract.py` + `report.py` — `enrich_finalize()`: extract usage/session/report from harness output, persist report artifact (subagent path only; called by `streaming_runner.py`)

**State flow:**
- Every spawn is append-only JSONL events in `~/.meridian/projects/<uuid>/spawns.jsonl` (runtime root, keyed by project UUID; falls back to repo `.meridian/` for uninitialized projects)
- Session state is JSONL events in `~/.meridian/projects/<uuid>/sessions.jsonl` (same runtime root)
- Work items are mutable JSON under `.meridian/work-items/` (repo-side, gitignored)
- All writes are atomic (tmp+rename) with `platform.locking.lock_file()` for concurrency (cross-platform: fcntl on POSIX, msvcrt on Windows)
- Crash recovery: the reaper (`lib/state/reaper.py`) detects orphaned spawns on read paths — heartbeat file recency (primary signal, 120s window), psutil `runner_pid` liveness (secondary, skipped for `finalizing` rows), and durable report completion. Active statuses: `queued`, `running`, `finalizing`. Terminal: `succeeded`, `failed`, `cancelled`. The `finalizing` state (entered via `mark_finalizing` CAS after harness exit) narrows the reap target to the drain/report window and enables `orphan_finalization` vs `orphan_run` distinction. Runner-origin terminal writes supersede reconciler-origin via the projection authority rule — no explicit recovery step needed.

**Mars integration:**
- `meridian mars ...` is a passthrough to the bundled `mars` binary
- Mars manages `.agents/` (agent profiles, skill content) from package sources
- Model alias resolution calls `mars models list --json` at resolve time
- See `mars/overview.md`

## Key Design Decisions

**Files-as-authority:** All state is files — no database, no hidden state. Runtime state (spawns, sessions, artifacts) lives under `~/.meridian/projects/<uuid>/`; repo scaffolding under `.meridian/`. To inspect spawns: `cat ~/.meridian/projects/<uuid>/spawns.jsonl | jq`. This makes the system inspectable, backup-friendly, and testable without service dependencies.

**Crash-only design:** No graceful shutdown path. Atomic writes (tmp+rename) mean every write either completes or leaves the prior state intact. JSONL readers skip truncated/malformed lines. The reaper runs on read paths, not a background daemon — `meridian status` is the recovery trigger.

**Harness-agnostic:** Meridian never assumes a specific AI harness. The adapter contract (`lib/harness/adapter.py:HarnessCapabilities`) declares what each harness supports (stream events, session resume, native skills, etc.). Command construction, output parsing, and session detection are adapter-private. Adding a harness = one adapter file + registry entry.

**Manifest as single source of truth:** `ops/commands.py` declares every operation once with name, description, input/output types, async+sync handlers, and surface membership (CLI and/or MCP). Both the CLI registration layer and the MCP server consume the manifest — no duplicated operation definitions.

**Config precedence:** CLI flags > ENV vars > agent profile > project config > user config > harness defaults. Each field resolves independently — a CLI model override forces harness re-derivation from the overridden model, not from the profile's harness.

**Workspace config:** Named `[workspace.NAME]` tables in `meridian.toml` (committed) and `meridian.local.toml` (local additions/overrides) declare sibling-repo roots projected to harness launches (`--add-dir` for Claude, `OPENCODE_CONFIG_CONTENT` env for OpenCode). `meridian workspace init` scaffolds `meridian.local.toml`. Invalid workspace blocks any spawn before harness contact. See `config/overview.md` and `launch/overview.md`.

## State Root Layout

State splits across two roots keyed by a per-project UUID. See `state/overview.md` for the full layout, path resolvers, and read-vs-write separation.

**Repo `.meridian/`** — committed scaffolding:
```
.meridian/
  id                    project UUID (committed; generated on first write)
  .gitignore            seeded/maintained non-destructively
  kb/                   agent-facing knowledge base (this directory)
  work/                 active work scratch dirs
  archive/work/         archived work scratch dirs
```

**User `~/.meridian/projects/<uuid>/`** — runtime state (local, never committed):
```
~/.meridian/projects/<uuid>/
  spawns.jsonl          append-only spawn events
  sessions.jsonl        append-only session events
  session-id-counter    monotonic counter for c1, c2, ...
  sessions/             per-session lock + lease files
  spawns/               per-spawn artifact dirs (<id>/prompt.md, report.md, ...)
  artifacts/            artifact blob store for spawn outputs (LocalStore)
  cache/                models.json cache, other transient data
```

UUID mapping: repo `.meridian/id` → runtime directory. Projects can be moved or renamed without losing runtime state. Platform user root defaults: `~/.meridian/` (Unix/macOS), `%LOCALAPPDATA%\meridian\` (Windows). Override: `MERIDIAN_PROJECT_ROOT` (absolute → full runtime root override; relative → repo-relative), `MERIDIAN_HOME` (user root default only).

## Surface Summary

| Surface | Entry point | Transport |
|---------|-------------|-----------|
| CLI | `meridian` (cyclopts) | stdio, human-readable |
| MCP | `meridian serve` (FastMCP) | stdio, JSON-RPC |
| Direct | `DirectAdapter` in-process | Python API, no subprocess |

The MCP surface exposes a subset of operations suited for programmatic agent use. Config operations and session/work management are CLI-only. Spawn create is available via the bare `meridian spawn` CLI command; continue is available via `meridian spawn --continue ...`. Both operations are also exposed as MCP tools.
