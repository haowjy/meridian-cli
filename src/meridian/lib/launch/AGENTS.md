# lib/launch/ — Composition and Execution

Sits between the policy layer (`ops/spawn/`) and mechanism layer (`harness/`).
Turns a `SpawnRequest` into a running process. Does not own policy decisions (what
to spawn) or harness specifics (how to build argv for Claude/Codex) — it owns the
composition seam that wires them together.

## The Composition Seam (Invariant I-1)

All launch state assembly — argv, env, permissions, prompt, workspace roots — happens
inside `build_launch_context()` in `context.py`. No driving adapter composes these
independently. This is a hard invariant; violation means two code paths can diverge silently.

`build_launch_context()` is a backward-compat wrapper over a two-phase pipeline:

```
SpawnRequest + LaunchRuntime
        │
        ▼
compile_prepared_policy_surface()     ← model/harness/profile resolution
        │
        ▼
prepare_launch_surface()              ← skill injection, prompt, content composition
        │                             (expensive: mars calls, profile loading)
        │  PreparedLaunchSurface
        │
        ▼
bind_launch_context()                 ← env, cwd, spec, argv, permissions
        │                             (cheap: microseconds)
        ▼
LaunchContext                         ← complete at construction; ready to run
```

**Why the split?** The primary CLI path calls `bind_launch_context()` twice: once
with `dry_run=True` for `--dry-run` display, then again with real spawn ID and paths.
`prepare_launch_surface()` is expensive and safe to call once. `bind_launch_context()`
is cheap and idempotent.

## Four Driving Adapters

Four code paths call `build_launch_context()` — each has a defined entry:

1. **Primary CLI** (`launch/__init__.py:launch_primary()`): prepare-once/bind-twice.
   First bind for dry-run display, second for `run_harness_process()`.
2. **Spawn subprocess** (`ops/spawn/execute.py`): foreground and background paths
   both converge at `launch_prepared_spawn()`.
3. **REST app** (`lib/app/spawn_routes.py`): resolve-before-persist via
   `SpawnApplicationService.prepare_spawn()`. Row created only on success.
4. **CLI streaming-serve** (`cli/streaming_serve.py`): also through
   `SpawnApplicationService.prepare_spawn()`, then `run_streaming_spawn()`.

## Key Types

- `SpawnRequest` / `LaunchRuntime` — frozen Pydantic DTOs; JSON-safe fields only.
  No `Path` on `SpawnRequest` — it gets persisted to disk by the background worker.
- `PreparedLaunchSurface` — preparation output; carries resolved request, skills, refs.
  Excludes spawn IDs, report paths, env — everything that varies per bind.
- `RuntimeBindings` — the only things that differ between preview and real bind:
  `spawn_id`, `report_output_path`, `runtime_work_id`, `chat_id`, `dry_run`.
- `LaunchContext` — fully composed; complete at construction, ready to hand to an executor.

## Finalization Ownership

Three concentric layers, each defined by function scope:
1. **Runner** (`execute_with_streaming`): `finally` handles partial-setup failures.
2. **Helper** (`launch_prepared_spawn`): `except` writes `launch_failure`; safe
   because `complete_spawn()` is idempotent.
3. **Surface backstop**: last-resort around the entire post-row section.

## Hard Invariants

| Code | Rule |
|---|---|
| I-1 | All composition inside `build_launch_context()` — no adapter composes independently |
| I-2 | No driving adapter reconstructs argv, env, or permissions independently |
| I-4 | `observe_session_id()` called exactly once post-execution (primary path only) |
| I-5 | `SpawnRequest`/`LaunchRuntime` carry no derived state; `LaunchContext` complete at construction |
| I-10 | Fork materialization (`fork.py`) happens only after spawn row exists |
| I-13 | `LaunchContext.warnings` is the sole channel for composition warnings |

## Composition Surfaces

`LaunchCompositionSurface` on `LaunchRuntime` controls which projection path runs:
- `PRIMARY` — interactive session
- `SPAWN_PREPARE` — subagent spawn; full prompt composition
- `DIRECT` — already-resolved request; skips policy resolution

Use `LaunchArgvIntent.SPEC_ONLY` on execution paths. `LaunchArgvIntent.REQUIRED` is
only for `ops/spawn/prepare.py` dry-run display (needs real argv for `cli_command`).
**Do not set `REQUIRED` on execution paths.**

## Key Entry Points

- `context.py` — `prepare_launch_surface()`, `bind_launch_context()` — the seam
- `__init__.py` — `launch_primary()` for the interactive primary path
- `streaming_runner.py` — `execute_with_streaming()` for spawn/streaming paths
- `process/` — `run_harness_process()` for the PTY/pipe primary executor

## Anti-Patterns

**Don't call `fork.materialize_fork()` before the spawn row exists.** Fork writes a
session artifact that references the spawn ID — the row must exist first (I-10).

**Don't route composition warnings to stderr or logs.** They go to
`LaunchContext.warnings` (I-13). Other channels are invisible to callers.

**Don't add `Path` fields to `SpawnRequest`.** The background worker serializes
`SpawnRequest` to disk — `Path` breaks JSON round-trip.

**Don't use stdlib `logging` in `launch/`.** Use `structlog.get_logger()`.
`capture_library_diagnostics()` captures stdlib warnings during spawn; structlog
bypasses it and leaks to stderr.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — workspace projection detail, env injection,
   background worker trust model, MERIDIAN_HARNESS child env behavior, full invariant table.

## Related

- `../ops/spawn/AGENTS.md` — policy layer that drives this layer
- `../harness/AGENTS.md` — mechanism layer this calls for `project_content()`, `build_launch_argv()`
- KB `architecture/launch-system.md` — full four-adapter diagram
- KB `concepts/spawn-lifecycle.md` — spawn status machine, crash recovery
