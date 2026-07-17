# platform/process_scope — Context

Process containment layer: one API seam, three platform backends, one fallback.
The parent `.context/` covers the high-level invariants; this file covers the
mechanism details specific to the sub-module.

## Architecture

Two dispatch entry points share the same routing logic but serve different call contexts:

```
# Async callers (launch paths)
ScopedProcessHandle.terminate()
  ├── containment == "posix_pgid"        → posix.terminate_pgid()
  ├── containment == "windows_job"       → fallback (handle threading pending)
  └── containment == "pid_tree_fallback" → fallback.terminate_tree()

# Sync callers (reaper, cancel, session-exit) — same dispatch, sync I/O
terminate_scope_sync(scope)
  ├── containment == "posix_pgid"        → posix.terminate_pgid()
  └── else                               → fallback.terminate_tree_sync()
```

Both entry points never raise — exceptions are caught and returned as a degraded
`CleanupResult` with `skip_reason="termination_exception"`. `ScopedProcessHandle.terminate()`
logs with `process_scope.terminate_failed`; `terminate_scope_sync` logs with
`terminate_scope_sync.failed`.

`terminate_scope_sync` sets `degraded_fallback=True` when the containment type is
anything other than `"pid_tree_fallback"` (i.e., posix_pgid fell through to fallback,
or windows_job routed to fallback) — matching the async path's degraded semantics exactly.

## Contracts

### PID Reuse Guard (PROC-006)

Every adapter checks `root_created_at_epoch` before sending any signal:

- If current process birth time differs from recorded by more than 1 second,
  the PID was reused — skip the kill and return `skip_reason="pid_reuse_detected"`.
- If the root process is absent (already dead), POSIX continues in degraded mode;
  the fallback treats an absent root as "already exited" and returns normally.

`root_created_at_epoch = 0.0` in the fallback is a sentinel meaning "unknown" —
the birth-time check is skipped when this sentinel is present.

### POSIX: Why Group Kill Is Stronger Than Single-PID Kill

`os.killpg(pgid, SIGTERM)` sends to every process in the group, including
descendants that have reparented to PID 1. Single-PID `SIGTERM` only reaches
the root; if the root has exited and its children reparented, they escape.

When the root is already dead before `terminate_pgid()` is called (degraded mode),
the function still attempts `os.killpg()`. If the group has also dissolved
(`ProcessLookupError`), a secondary full-table scan by PGID runs to catch orphaned
processes that stayed in the group after reparenting (PROC-004 sweep).

### Windows: Job Object Handle Lifetime

`assign_to_new_job(pid)` returns `(job_name, job_handle)`. The handle **must
stay alive** until the scope should be released — `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
kills all job members when the handle closes. Callers must hold the handle in
a live Python object; letting it get garbage-collected releases the containment
boundary early.

Current status: `ScopedProcessHandle` with `containment="windows_job"` falls
back to psutil tree kill because handle threading into the handle is not yet
wired through. `degraded_fallback=True` in the result flags this path.

### Fallback Path Limitations

`terminate_tree()` / `terminate_tree_sync()` take a snapshot of the process tree
with `psutil.children(recursive=True)` before sending signals. If an intermediate
exits between snapshot and signal, its children drop from the tree and escape.
This snapshot race is accepted for the fallback path — it is better than nothing
but weaker than group kill or Job Object containment.

## Patterns

- Use `ScopedProcessHandle.terminate()` from async launch callers — it handles
  containment dispatch, logging, and exception safety in one call.
- Use `terminate_scope_sync(scope, ...)` from sync callers that have a
  `ProcessScopeSnapshot` (reaper, cancel, session-exit). This is the correct
  sync path for all scope-aware termination; it routes to the right backend
  based on `scope.containment`.
- Use `terminate_tree_sync(pid, ...)` only when there is no scope metadata —
  legacy worker-pid fallback and hook contexts (git-autosync) where a
  `ProcessScopeSnapshot` is unavailable.
- Do not call `posix.terminate_pgid()` or `windows_job.terminate_job()` directly
  from outside this package — `ScopedProcessHandle.terminate()` and
  `terminate_scope_sync()` are the two integration entry points.

## Related

- [`../AGENTS.md`](../AGENTS.md) — module entry points
- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — parent platform invariants
- [`../../../core/.context/CONTEXT.md`](../../../core/.context/CONTEXT.md) — process_cleanup.py, which owns policy decisions and calls terminate_scope_sync()
- KB: `$MERIDIAN_CONTEXT_KB_DIR/architecture/process-scope.md` (see `meridian context kb`)
