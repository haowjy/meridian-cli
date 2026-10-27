# platform/process_scope/

Process containment: the whole subprocess tree Meridian starts dies when a spawn ends,
cancels, or Meridian crashes. Single-PID kill is not enough; reparented children escape it.

| `containment` | Backend | Mechanism |
|---|---|---|
| `posix_pgid` | `posix.py` | `os.killpg()` on the process group |
| `windows_job` | legacy Job Object (untested; don't extend) | handle-owned termination |
| `pid_tree_fallback` | `fallback.py` | psutil tree snapshot, SIGTERM → SIGKILL |

## Rules

- **Terminate through `ScopedProcessHandle.terminate()`**; never call
  `posix.terminate_pgid()` or `windows_job.terminate_job()` from outside the package. It
  must not raise: failures are logged (`process_scope.terminate_failed`) and returned as a
  degraded `CleanupResult`. From hooks and synchronous teardown with no event loop use
  `fallback.terminate_tree_sync()`.
- **Capture `scope_snapshot` before `connection.stop()`**; `stop()` clears both it and
  `subprocess_pid`, and without the snapshot the containment boundary is lost.
- **Every adapter validates `root_created_at_epoch` before signalling.** A birth time more
  than 1 second off means PID reuse: skip with `skip_reason="pid_reuse_detected"`. `0.0`
  means unknown and skips the check.
- **Prefer POSIX group kill**: the group reaches descendants reparented to PID 1; the psutil
  fallback is degraded. Scope roots are launch shims, so the PGID is the cleanup handle.
  `pgid_reachable` in reaper diagnostics is best-effort only (signal 0 counts zombies and
  has no group birth-time guard).

[.context/CONTEXT.md](.context/CONTEXT.md) — PID reuse guard, orphan sweep, Job Object
handle lifetime · [../.context/CONTEXT.md](../.context/CONTEXT.md)
