# lib/platform/ — Context

Centralizes all OS-specific code. Linux and macOS are supported. Native-Windows
branches are legacy, untested, and must not be expanded.

## Contracts

### Import Rule: No Platform-Specific Imports Outside This Module

POSIX-only stdlib modules (`fcntl`, `pty`, `termios`, `tty`) and Windows-only
modules (`msvcrt`) must not be imported at module top-level outside `lib/platform/`.
Existing OS-specific boundaries use two patterns:

**Pattern A — Deferred proxy** (shared use from multiple callers):
```python
from meridian.lib.platform import fcntl, pty   # deferred until first use
```
These are `DeferredUnixModule` instances defined in `unix_modules.py` and re-exported
from `__init__.py`. The legacy native-Windows branch defers `ImportError` until a
proxy is called; this branch is untested.

**Pattern B — Function-local import** (existing Windows-only call site):
```python
def _acquire_windows_lock(handle):
    import msvcrt as _msvcrt   # Windows-only, scoped to Windows code path
```

Never add a new top-level `import fcntl` or `import msvcrt` outside this module.

### OS Detection

```python
from meridian.lib.platform import IS_WINDOWS, IS_POSIX
```

Do not add or expand native-Windows branches. Keep necessary OS detection
centralized here; when maintaining an existing branch, prefer these over inline
`sys.platform` comparisons.

### Home Directory

`get_home_path()` checks `HOME` first, then falls back to `Path.home()`. Always
use it instead of `Path.home()` directly so path resolution remains explicit and
testable.

## File Locking (`locking.py`)

`lock_file(path)` is a context manager yielding an exclusive advisory lock.

**Thread-local reentrancy:** a thread that already holds the lock re-enters without
deadlocking. The depth counter is per-thread; the OS lock is released only on
outermost `__exit__`.

**POSIX:** `fcntl.flock(LOCK_EX)` — blocking until acquired.

**Legacy Windows branch (untested):** `msvcrt.locking(LK_NBLCK, 1)` with a 50ms
retry loop. It requires a non-zero-length file, writes a guard byte, and locks one
byte at offset 0.

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
reparented descendants that single-PID kill misses. Linux installs
`PR_SET_PDEATHSIG` before exec when available. POSIX platforms without a native
parent-death signal (macOS/BSD, or Linux without `prctl`) launch a detached
watchdog helper after spawn. The helper acknowledges readiness over an inherited
pipe before containment is recorded, watches the Meridian launcher by
birth-validated PID, and terminates the full target process group if the launcher
disappears first. It remains alive while any non-zombie process-group member exists,
even if the scope root exits before its descendants.

**Legacy Windows branch (untested):** Job Object setup exists, but scoped
termination currently routes to the psutil fallback because Job Object handle
threading is not wired through.

**Invariants:**
- Scope snapshot must be persisted **before** `connection.stop()` — both connections
  clear `subprocess_pid` and `scope_snapshot` inside `stop()`.
- `terminate()` must be exception-safe — teardown must not throw.
- All terminate paths validate `root_created_at_epoch` against current process birth
  time to guard against PID reuse before sending any signal.

`terminate.py` is a backward-compat shim that delegates to `process_scope/fallback.py`.
Existing callers (`runner_helpers.py`) work unchanged.

## Legacy Windows Signal Behavior (Untested)

Three deviations from POSIX that cause silent failures if ignored:

**`os.kill(pid, SIGINT)` is unreliable.** CPython maps it to `GenerateConsoleCtrlEvent(CTRL_C_EVENT)`,
which only works when the target shares the same console process group. Existing
branches use `process.terminate()` instead.

**`loop.add_signal_handler()` is a no-op on Windows.** `asyncio.ProactorEventLoop`
doesn't implement it. Existing branches use `signal.signal()` +
`loop.call_soon_threadsafe()` instead:
```python
def _handle(signum, frame):
    loop.call_soon_threadsafe(shutdown_event.set)
signal.signal(signal.SIGINT, _handle)   # must call from main thread
```

**`os.kill(pid, SIGTERM)` works but is not graceful.** CPython maps it to
`TerminateProcess()` — unconditional kill, no signal handler runs. It does not
silently fail. Existing branches use `psutil.Process(pid).terminate()` instead.
