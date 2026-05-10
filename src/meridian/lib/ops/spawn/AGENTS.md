# ops/spawn/

Spawn operation policy layer. Surfaces spawn lifecycle operations to CLI and
MCP — create, list, show, cancel, wait, fork, continue. Policy (what to do,
access control, validation) lives here; mechanism (process execution, argv
composition) lives in `launch/`.

## Entry Points

- `api.py` — all spawn operations: `spawn_create_sync`, `spawn_list_sync`, `spawn_cancel_sync`,
  `spawn_wait_sync`, `spawn_fork_sync`, `spawn_continue_sync`, and async variants
- `models.py` — input/output models: `SpawnCreateInput`, `SpawnActionOutput`, `SpawnWaitInput`,
  `SpawnDetailOutput`, `SpawnWaitMultiOutput`, etc.

## Key Files

- `execute.py` — `execute_spawn_blocking()`, `execute_spawn_background()`: foreground and background
  execution paths; both converge at `launch_prepared_spawn()`
- `prepare.py` — `build_create_payload()`, `validate_create_input()`: pre-execution dry-run display
  (uses `LaunchArgvIntent.REQUIRED` — the only path that does)
- `query.py` — read spawn records, read written files, `resolve_spawn_reference()`
- `context_ref.py` — `resolve_context_ref()` helpers for `-f` references
- `failure_policy.py` — depth guard, depth limit helpers, depth-exceeded output

## Architecture

```
CLI / MCP surface
      │
      ▼
ops/spawn/api.py       ← policy: depth check, work attachment, validate, route to exec path
      │
      ├── execute.py   ← blocking or background execution
      └── prepare.py   ← dry-run argv resolution only
            │
            ▼
      launch/          ← composition + execution mechanism
```

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — depth guard, wait checkpoint behavior,
  no-arg wait scoping, fork/continue cross-harness guard, background worker trust model

## Related

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — ops/ policy layer overview; execution path detail
→ [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — mechanism this layer drives
