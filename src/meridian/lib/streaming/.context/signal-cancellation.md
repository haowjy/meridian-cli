# lib/streaming/ — Signal Cancellation Context

Cancellation dispatch and scope cleanup for streaming spawns. The generic streaming
runtime remains in [CONTEXT.md](CONTEXT.md).

## SignalCanceller

`SignalCanceller` (`signal_canceller.py`) is the two-lane cancel dispatcher: CLI spawns
and app-managed spawns take different paths, but both converge on a terminal state read.

### Cancel Dispatch

`cancel()` routes by `launch_mode`:

- **CLI spawns** → `_cancel_cli_spawn()` — resolves runner PID first, signals it;
  falls back to scope cleanup only when the runner is absent or dead.
- **App spawns** → `_cancel_app_spawn()` — delegates to `SpawnManager.stop_spawn()` if a
  manager is present; falls back to an HTTP cancel against the running app socket otherwise.

`SignalCanceller` does **not** claim terminal lifecycle authority. It delivers cancel
signals and returns delivery facts (`finalizing: bool`, `already_terminal: bool`).
The `SpawnApplicationService.cancel()` path owns convergence to terminal — it calls
`_force_cancel_convergence()` when delivery alone is insufficient.

### `_cancel_cli_spawn()` — Runner-First, Scope-Fallback Path

The method resolves `record.runner_pid` first and signals that runner tree. If the
runner is absent/dead, or if runner termination does not produce terminal state, it
falls back to `process_cleanup.terminate_spawn_scopes()` via `asyncio.to_thread()`
when no live runner is available. After a guarded runner-tree signal, it calls
`process_cleanup.terminate_recorded_spawn_scopes()` so real scope records still clean
up without re-running the legacy worker fallback. The cleanup path owns scope policy:

1. Reads scope sidecars via `read_scopes_from_disk()`.
2. Skips already-released scopes by concrete `release_id`.
3. Preserves live `session_owned` scopes via `should_skip_cleanup()`.
4. Terminates remaining scopes through `terminate_scope_sync()` and marks each
   concrete `release_id` released.
5. Falls back to legacy `worker_pid` termination only through `terminate_spawn_scopes()`
   when no sidecars exist.

After signal delivery, `_wait_for_terminal()` polls the spawn record for up to
`grace_seconds`. If the record never reaches a terminal status, the outcome carries
`finalizing=True` — the caller must not treat this as a confirmed stop.

### Dependency Direction

`signal_canceller` depends on `platform/` and `state/` — it does not depend on
`core.spawn_service` or own lifecycle finalization. The cancel path has a live event
loop and manages scope cleanup inline. `core.process_cleanup` is the sync-only
reclamation path used at startup for orphan recovery — the two paths don't share
scope management logic.

## Anti-Pattern

- **Don't add scope cleanup after `SignalCanceller.cancel()`** — the canceller handles scope termination internally via the scope-sidecar path. Duplicate cleanup causes double-kill races.
