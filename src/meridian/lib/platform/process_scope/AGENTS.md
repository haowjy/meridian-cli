# platform/process_scope/

Platform-specific process containment layer. Ensures the entire subprocess tree
Meridian starts is killed on spawn completion, cancel, or crash.

## Files

- `base.py` — shared types: `ProcessScopeSnapshot`, `ScopedProcessHandle`, `CleanupResult`
- `posix.py` — POSIX adapter: `terminate_pgid()` via `os.killpg()`
- `windows_job.py` — Windows adapter: `assign_to_new_job()` + `terminate_job()` via Job Object
- `fallback.py` — cross-platform degraded path: `terminate_tree()` + `terminate_tree_sync()` via psutil

## Entry Points

For launch callers: construct a `ScopedProcessHandle` wrapping the asyncio `Process`
and a `ProcessScopeSnapshot`, then call `handle.terminate()`. The handle dispatches to
the right adapter based on `snapshot.containment`.

For teardown without a handle (reaper, legacy paths): `terminate_tree()` (async) or
`terminate_tree_sync()` (sync, no event loop required).

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Why POSIX group kill is stronger than single-PID kill for reparented descendants
- Windows Job Object handle-lifetime requirement
- PID reuse guard mechanics (PROC-006)
- Fallback path limitations and when it activates

## Related

- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — parent platform module contracts
- KB: [architecture/process-scope.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/process-scope.md)
