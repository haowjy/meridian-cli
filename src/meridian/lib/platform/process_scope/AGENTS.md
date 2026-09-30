# platform/process_scope/

Process containment: ensures the entire subprocess tree Meridian starts is killed
when a spawn ends, whether that's a clean completion, a cancel, or a Meridian crash.
Single-PID kill is not enough — reparented children escape it.

Three backends behind one API seam (`ScopedProcessHandle.terminate()`):

The Windows backend is legacy, untested code retained behind the platform seam;
do not extend it as product support.

| `containment` | Backend | Mechanism |
|---|---|---|
| `posix_pgid` | `posix.py` | `os.killpg()` — kills the entire process group |
| `windows_job` | legacy fallback | Job Object helpers exist; dispatch uses the psutil fallback |
| `pid_tree_fallback` | `fallback.py` | psutil tree snapshot + SIGTERM → SIGKILL |

## Key Rules

**Use `ScopedProcessHandle.terminate()` from async launch callers.** It handles
dispatch, exception safety, and logging. Do not call `posix.terminate_pgid()` or
`windows_job.terminate_job()` directly from outside this package.

**Capture `scope_snapshot` before `connection.stop()`.** Both `subprocess_pid` and
`scope_snapshot` are cleared inside `stop()`. The terminate call needs the pre-captured
snapshot — if you call terminate after stop without a snapshot, the containment boundary
is lost.

**`terminate()` must not raise.** Exceptions are caught, logged with
`process_scope.terminate_failed`, and returned as a degraded `CleanupResult`.
Teardown paths must remain safe even when containment fails.

**Every adapter validates `root_created_at_epoch` before sending any signal.** If the
current process birth time differs from the recorded value by more than 1 second, the
PID was reused — the kill is skipped with `skip_reason="pid_reuse_detected"`. A sentinel
value of `0.0` means "unknown" — the check is skipped.

## Why POSIX Group Kill

`os.killpg(pgid, SIGTERM)` sends to every process in the group, including descendants
that have reparented to PID 1. `SIGTERM` to the root PID only reaches the root — if
it's already dead, reparented children escape entirely. Use POSIX containment when
available; psutil fallback is accepted as degraded.

Scope roots are launch shims and therefore identify what Meridian launched; they are
not guaranteed to remain the serving backend process. The PGID is the cleanup handle
that contains shim descendants, including reparented children. Reaper diagnostics
report `pgid_reachable` as best-effort evidence only: signal 0 counts zombies, has no
process-group birth-time reuse guard, and cannot see a child that changes its own
process group.

## Legacy Windows Job Object Handle Lifetime

`assign_to_new_job(pid)` returns `(job_name, job_handle)`. The handle must stay alive —
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` kills all job members when the handle is garbage
collected. Letting the handle go early releases the containment boundary silently.

## Entry Points

- `base.py` — `ScopedProcessHandle`, `ProcessScopeSnapshot`, `CleanupResult`
- `posix.py` — `terminate_pgid()` (POSIX backend)
- `windows_job.py` — `assign_to_new_job()`, `terminate_job()` (Windows backend)
- `fallback.py` — `terminate_tree()` (async), `terminate_tree_sync()` (sync, no event loop)

Use `terminate_tree_sync()` from hook contexts and synchronous teardown paths where no
event loop is running.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — PID reuse guard mechanics (PROC-006),
POSIX orphan sweep (PROC-004), legacy Job Object status, fallback snapshot race

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — parent platform invariants and import rules

## Related

- KB: `$MERIDIAN_CONTEXT_KB_DIR/architecture/process-scope.md` (see `meridian context kb`)
