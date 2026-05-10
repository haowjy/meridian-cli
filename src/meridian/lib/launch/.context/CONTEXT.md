# launch/ — Composition and Execution

## Architecture

### The Composition Seam (Invariant I-1)

All launch state composition happens inside `build_launch_context()` in `context.py`.
No driving adapter composes argv, env, or permissions independently. This is a hard
invariant — violation means two places can diverge silently.

`build_launch_context()` is now a backward-compat wrapper over a two-phase pipeline:

```
SpawnRequest + LaunchRuntime
        │
        ▼
compile_prepared_policy_surface()  ← model/harness/profile resolution (before prepare)
        │
        ▼
prepare_launch_surface()           ← skill injection, prompt, content composition
        │
        │  PreparedLaunchSurface (frozen dataclass — public boundary)
        │
        ▼
bind_launch_context()              ← env, cwd, spec, argv, permissions
        │
        ▼
LaunchContext                      ← complete at construction; ready to run
```

**Why the split?** The primary CLI path needs to call `bind_launch_context()` twice:
once with `dry_run=True` for `--dry-run` display, then again with real spawn-ID and
paths for actual execution. `prepare_launch_surface()` is expensive (mars calls, profile
loading, prompt assembly) and safe to do once before the session is opened.
`bind_launch_context()` is cheap (microseconds) and called as many times as needed.

### PreparedLaunchSurface

The in-memory boundary between preparation and binding. Carries: resolved request,
harness, composition warnings, loaded references, agent inventory prompt, context
prompt, alias catalog, model selection context. Deliberately excludes spawn IDs,
report paths, env, argv, and permission outputs — everything that varies per bind.

`launch_request` on `PreparedLaunchSurface` preserves the original pre-resolution
request for `LaunchContext.request` compatibility. `request` carries the resolved
version used by `bind_launch_context()`.

### RuntimeBindings

Frozen dataclass for runtime-only values: `spawn_id`, `report_output_path`,
`runtime_work_id`, `chat_id`, `forked_harness_session_id`, `plan_overrides`, `dry_run`.
These are the only things that differ between the preview bind and the real bind.

### Four Driving Adapters

Four paths call into this module — each converges on `build_launch_context()`:

1. **Primary CLI** (`launch/__init__.py:launch_primary()`): prepare-once/bind-twice.
   Dry-run preview uses the first bind; `run_harness_process()` rebuilds with real paths.

2. **Spawn subprocess** (`ops/spawn/execute.py`): foreground calls
   `execute_spawn_blocking()` → `launch_prepared_spawn()` → `execute_with_streaming()`;
   background persists `BackgroundWorkerLaunchRequest` and detaches a worker subprocess
   which calls `_execute_existing_spawn()` → `launch_prepared_spawn()`.
   Both foreground and background converge at `launch_prepared_spawn()`, which owns
   pre-run failure finalization.

3. **REST app** (`lib/app/spawn_routes.py`): `SpawnApplicationService.prepare_spawn()`
   implements resolve-before-persist — `build_launch_context()` runs before any spawn
   row is created. Row is only created on success (SEAM-1).

4. **CLI streaming-serve** (`cli/streaming_serve.py`): also uses
   `SpawnApplicationService.prepare_spawn()`, then calls `run_streaming_spawn()`
   directly from `streaming_runner.py`.

### Finalization Ownership Layers

Three concentric layers inside the spawn path, each defined by function scope:

1. **Runner** (`execute_with_streaming`): sentinel locals initialized to `None`;
   `finally` block handles partial-setup failures.
2. **Helper** (`launch_prepared_spawn`): `except` catches pre-run exceptions, writes
   `launch_failure`; safe because runner's `complete_spawn()` is idempotent.
3. **Surface backstop**: last-resort around the entire post-row section in the
   calling surface function.

## Contracts

### DTO Discipline

- `SpawnRequest` and `LaunchRuntime`: frozen Pydantic, JSON-safe field types only.
  No `Path` on `SpawnRequest`. No `arbitrary_types_allowed`.
- `LaunchContext`: frozen dataclass. No pre-composed intermediate DTOs.
- No derived/cached state on any DTO — factory recomputes from inputs on each call.

Adding mutable state or `Path` fields to `SpawnRequest` breaks the JSON-safe
constraint required for the background worker's disk-persisted request.

### Invariants (from KB `architecture/launch-system.md`)

| Code | Rule |
|------|------|
| I-1 | All composition inside `build_launch_context()` — no adapter composes independently |
| I-2 | No driving adapter reconstructs argv, env, or permissions independently |
| I-4 | `observe_session_id()` called exactly once post-execution (primary path only) |
| I-5 | `SpawnRequest`/`LaunchRuntime` carry no derived state; `LaunchContext` complete at construction |
| I-10 | Fork materialization (`fork.py`) happens only after spawn row exists |
| I-13 | `LaunchContext.warnings` is the sole channel for composition warnings |

These are checked on every PR touching `launch/`, `harness/`, `ops/spawn/`, `app/`,
or `cli/streaming_serve.py`.

### Composition Surfaces

`LaunchCompositionSurface` on `LaunchRuntime` controls which projection path runs
inside `prepare_launch_surface()`:

- `PRIMARY` — interactive session; uses `harness.seed_session()`, normalizes passthrough args
- `SPAWN_PREPARE` — subagent spawn; full prompt composition + reference loading
- `DIRECT` — already-resolved request; skips policy resolution entirely

`SPAWN_PREPARE` uses `LaunchArgvIntent.SPEC_ONLY` for execution paths; `LaunchArgvIntent.REQUIRED`
only for `ops/spawn/prepare.py` dry-run display (needs a real argv for `cli_command`).
Do not set `REQUIRED` on execution paths.

### MERIDIAN_HARNESS Child Env

`bind_launch_context()` writes `MERIDIAN_HARNESS = harness.id.value` into the child env.
This is **one-hop only** — not in `ALLOWED_CHILD_ENV_KEYS`, does not cascade to
grandchildren. Each spawn level derives its own value from its own `build_launch_context()`.

The orchestrator reads `os.getenv("MERIDIAN_HARNESS")` at wait time to determine
its own yield interval — it is asking about *its own* harness's prompt-cache TTL,
not the spawns it is waiting on.

### Workspace Projection

Two steps inside `bind_launch_context()`:

1. **Gate**: `resolve_workspace_snapshot_for_launch()` raises on `"invalid"` workspace.
   `"none"` and `"present"` pass through.
2. **Projection**: `project_workspace_roots()` maps roots per harness — Claude/Codex get
   `--add-dir` args; OpenCode gets `OPENCODE_CONFIG_CONTENT` env override (deep-merged).

Roots include workspace roots, git context clone roots, context projection roots
(work, kb, extras), runtime root, and system temp dir — deduplicated in order.
Extra args remain user-owned passthrough; they are not a workspace projection channel.

### Logging Convention

`launch/` uses `structlog.get_logger()`. Do not use stdlib `logging` here —
the split matters because `capture_library_diagnostics()` (wrapping `build_launch_context()`)
captures stdlib warnings during spawn; structlog bypasses it and would leak to stderr.

## Patterns

**Never call `fork.materialize_fork()` before the spawn row exists.** Fork writes
a session artifact referencing the spawn — the row must exist first (I-10).

**Warnings go to `LaunchContext.warnings`, not stderr or logs.** Adapters that
surface composition issues through other channels violate I-13 and make warnings
invisible to callers.

**Background worker trusts the persisted `BackgroundWorkerLaunchRequest`.** It does
not re-resolve model or harness from the spawn record. The request is already fully
resolved when persisted. Empty `model` is accepted — model-optional profiles exist.

## Related KB

- `architecture/launch-system.md` — full adapter diagram, prepare/bind split detail, module map
- `concepts/spawn-lifecycle.md` — spawn status machine, crash recovery, authority lattice
- `architecture/spawn-finalization.md` — finalization policy, per-spawn lock, `CompleteSpawnOutcome`

## Lateral Links

- `../ops/spawn/.context/CONTEXT.md` — how `ops/spawn/` drives this layer
- `../harness/` — adapters this layer calls into for `project_content()`, `preflight()`, `build_launch_argv()`
