# ops/spawn/ — Spawn Policy Layer

The policy half of spawn creation and execution. Surfaces `spawn_create`, `spawn_list`,
`spawn_cancel`, `spawn_wait`, `spawn_fork`, `spawn_continue` to CLI and MCP.
Does not compose argv or env — that's `launch/`. Owns: depth checks, work attachment,
input validation, and routing to foreground vs background execution.

## Execution Paths

Both paths converge at `launch_prepared_spawn()`:

**Foreground:** `execute_spawn_blocking()` → creates spawn row → calls
`launch_prepared_spawn()` via `asyncio.run()` → `execute_with_streaming()`.

**Background:** `execute_spawn_background()` → creates spawn row → persists
`BackgroundWorkerLaunchRequest` to disk → detaches subprocess. Worker calls
`_execute_existing_spawn()` → `launch_prepared_spawn()`.

`launch_prepared_spawn()` owns pre-run failure finalization. If it throws, it writes
`launch_failure` before re-raising. This is safe because `complete_spawn()` is idempotent.

## Depth Guard

`api.py` checks `max_depth_reached()` before every spawn execution. A spawn chain
that exceeds the depth limit returns `depth_exceeded_output()` instead of creating
a new spawn. `MERIDIAN_DEPTH` is an env var that increments with each spawn level;
nested processes also read it to skip reaping side effects.

## Wait Behavior

`spawn_wait` has two modes:
- With spawn IDs: waits for specific spawns.
- Without args (no-arg form): scoped to the current `MERIDIAN_DEPTH` — waits for
  spawns created at this nesting level only, not grandchild spawns.

Checkpoint polling respects the depth-appropriate yield interval (read from
`MERIDIAN_HARNESS` env — the orchestrator's own harness prompt-cache TTL).

## The Exception to SPEC_ONLY

`prepare.py` uses `LaunchArgvIntent.REQUIRED`. This is the only execution path
that needs real argv — it populates `cli_command` for dry-run display. All actual
execution paths use `SPEC_ONLY`. Do not set `REQUIRED` on execution paths.

## Fork/Continue Cross-Harness Guard

Fork and continue operations check that the target spawn's harness matches the
current harness context. Cross-harness fork/continue is not supported — the guard
raises before any state is created.

## Key Entry Points

- `api.py` — all spawn operations (sync and async variants).
- `models.py` — `SpawnCreateInput`, `SpawnDetailOutput`, `SpawnWaitInput`, etc.
- `execute.py` — re-exporter for public entry points (`execute_spawn_blocking`,
  `execute_spawn_background`) and depth guard helpers. Implementation split across:
  `execute_init.py` (spawn record init), `execute_bg.py` (background worker),
  `execute_runner.py` (blocking path), `execute_session.py` (session management).
  Import from `execute.py` only — do not import `execute_*` modules from outside `ops/spawn/`.
- `prepare.py` — `build_create_payload()` for dry-run display only.
- `query.py` — `resolve_spawn_reference()`, read spawn records and written files.
- `context_ref.py` — `resolve_context_ref()` for `-f` references.
- `failure_policy.py` — depth guard, depth limit helpers.

## Anti-Patterns

**Don't create spawn rows before calling `build_launch_context()` on new code paths.**
The existing subprocess path does this (known gap); don't replicate the pattern. New
paths should use resolve-before-persist (REST/streaming model).

**Don't set `LaunchArgvIntent.REQUIRED` on execution paths.** Only `prepare.py` gets
a real argv — execution paths use `SPEC_ONLY`.

## Depth

→ [.context/CONTEXT.md](../.context/CONTEXT.md) — wait checkpoint behavior, no-arg
   wait scoping, background worker trust model, SEAM-1/2/3 contracts.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — ops/ policy layer overview;
  SpawnApplicationService layer diagram; execution path ownership.
- [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — mechanism
  layer this drives.
