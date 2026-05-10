# lib/platform/ — Context

Centralizes all OS-specific code. Windows is a first-class product requirement —
validate Windows behavior for any change touching paths, processes, or locking.

## Contracts

### Import Rule: No Platform-Specific Imports Outside This Module

POSIX-only stdlib modules (`fcntl`, `pty`, `termios`, `tty`) and Windows-only
modules (`msvcrt`) must not be imported at module top-level outside `lib/platform/`.
Two patterns are approved:

**Pattern A — Deferred proxy** (shared use from multiple callers):
```python
from meridian.lib.platform import fcntl, pty   # safe on Windows — forwards on first use
```
These are `DeferredUnixModule` instances defined in `unix_modules.py` and re-exported
from `__init__.py`. They raise `ImportError` on Windows only when actually called,
not at import time.

**Pattern B — Function-local import** (single call site, Windows-only):
```python
def _acquire_windows_lock(handle):
    import msvcrt as _msvcrt   # Windows-only, scoped to Windows code path
```

Never add a new top-level `import fcntl` or `import msvcrt` outside this module.

### OS Detection

```python
from meridian.lib.platform import IS_WINDOWS, IS_POSIX
```

Prefer these over `sys.platform == "win32"` comparisons anywhere in the codebase.
Inline `sys.platform` checks are a refactor trigger.

### Home Directory

`get_home_path()` checks `HOME` env var first, falls back to `Path.home()`. On
Windows, `Path.home()` ignores `HOME` and queries Windows APIs — breaking test
isolation. Always use `get_home_path()`, never `Path.home()` directly.

## File Locking (`locking.py`)

`lock_file(path)` is a context manager yielding an exclusive advisory lock.

**Thread-local reentrancy:** a thread that already holds the lock re-enters without
deadlocking. The depth counter is per-thread; the OS lock is released only on
outermost `__exit__`.

**POSIX:** `fcntl.flock(LOCK_EX)` — blocking until acquired.

**Windows:** `msvcrt.locking(LK_NBLCK, 1)` with 50ms retry loop. Requires a
non-zero-length file; implementation writes a guard byte and fsyncs before the first
lock attempt. Always locks exactly 1 byte at offset 0.

`try_lock_file(path)` is non-blocking and yields `None` if the lock is already held.

## Process-Scope Containment (`process_scope/`)

Containment ensures the entire subprocess tree is killed when a spawn ends, even if
Meridian itself dies mid-flight.

```
process_scope/
  base.py          — ScopedProcessHandle, ProcessScopeSnapshot, CleanupResult types
  posix.py         — setsid() / process-group kill
  windows_job.py   — Job Object create/assign/terminate (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
  fallback.py      — psutil children(recursive=True) + SIGTERM → SIGKILL (degraded)
```

**POSIX:** `setsid()` at launch; `os.killpg(pgid, SIGTERM)` on teardown. Catches
reparented descendants that single-PID kill misses.

**Windows:** Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. When Meridian
exits, the OS closes the handle and kills all assigned processes automatically.

**Invariants:**
- Scope snapshot must be persisted **before** `connection.stop()` — both connections
  clear `subprocess_pid` and `scope_snapshot` inside `stop()`.
- `terminate()` must be exception-safe — teardown must not throw.
- All terminate paths validate `root_created_at_epoch` against current process birth
  time to guard against PID reuse before sending any signal.

`terminate.py` is a backward-compat shim that delegates to `process_scope/fallback.py`.
Existing callers (`runner_helpers.py`) work unchanged.

## Windows Signal Behavior

Three deviations from POSIX that cause silent failures if ignored:

**`os.kill(pid, SIGINT)` is unreliable.** CPython maps it to `GenerateConsoleCtrlEvent(CTRL_C_EVENT)`,
which only works when the target shares the same console process group. Subprocesses
launched with `CREATE_NEW_PROCESS_GROUP` never receive it. Use `process.terminate()`
on Windows instead.

**`loop.add_signal_handler()` is a no-op on Windows.** `asyncio.ProactorEventLoop`
doesn't implement it. Use `signal.signal()` + `loop.call_soon_threadsafe()` instead:
```python
def _handle(signum, frame):
    loop.call_soon_threadsafe(shutdown_event.set)
signal.signal(signal.SIGINT, _handle)   # must call from main thread
```

**`os.kill(pid, SIGTERM)` works but is not graceful.** CPython maps it to
`TerminateProcess()` — unconditional kill, no signal handler runs. It does not
silently fail. Use `psutil.Process(pid).terminate()` for equivalent cross-platform
behavior.

## Related KB

- [KB: Platform Abstractions](../../../../../../../../.meridian/git/meridian-flow-docs/kb/codebase/platform-abstractions.md) — cross-cutting platform design
