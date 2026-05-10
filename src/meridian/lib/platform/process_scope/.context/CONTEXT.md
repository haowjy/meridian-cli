# platform/process_scope — Context

Process containment layer: one API seam, three platform backends, one fallback.
The parent `.context/` covers the high-level invariants; this file covers the
mechanism details specific to the sub-module.

## Architecture

```
ScopedProcessHandle.terminate()
  ├── containment == "posix_pgid"   → posix.terminate_pgid()
  ├── containment == "windows_job"  → fallback (handle threading pending)
  └── containment == "pid_tree_fallback"
                                    → fallback.terminate_tree()
```

`terminate()` on `ScopedProcessHandle` never raises — exceptions are caught,
logged with `process_scope.terminate_failed`, and returned as a degraded
`CleanupResult` with `skip_reason="termination_exception"`.

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
  dispatch, logging, and exception safety in one call.
- Use `terminate_tree_sync()` from hook contexts (no event loop): git-autosync,
  process cleanup in synchronous teardown paths.
- Do not call `posix.terminate_pgid()` or `windows_job.terminate_job()` directly
  from outside this package — `ScopedProcessHandle` is the integration point.

## Related

- [`../AGENTS.md`](../AGENTS.md) — module entry points
- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — parent platform invariants
- KB: [architecture/process-scope.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/process-scope.md)
