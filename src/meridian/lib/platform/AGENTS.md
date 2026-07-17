# lib/platform/

All platform-specific OS code lives here. Everything else in the codebase should
be platform-agnostic — if you're writing `sys.platform == "win32"` outside this
module, that's a refactor trigger.

Windows is a first-class product target. Validate Windows behavior for any change
touching paths, processes, locking, or signal handling. Do not ship code that only
works on POSIX.

## Mental Model

Two layers:

1. **Detection and primitives** (`__init__.py`, `locking.py`, `terminate.py`) —
   `IS_WINDOWS`, `IS_POSIX`, `get_home_path()`, file locking, process-tree teardown shim.

2. **Process containment** (`process_scope/`) — ensures the full subprocess tree
   Meridian starts is killed on spawn completion, cancel, or crash. Three backends:
   POSIX group kill, Windows Job Object, and a psutil fallback.

## Import Rule: Never Import POSIX Modules Outside This Package

`fcntl`, `pty`, `termios`, `tty` are POSIX-only. `msvcrt` is Windows-only. Either:

- **Deferred proxy** (shared callers): `from meridian.lib.platform import fcntl, pty`
  — these are `DeferredUnixModule` instances that fail only when *called* on Windows,
  not at import time.
- **Function-local import** (single Windows-only path): `import msvcrt as _msvcrt`
  inside the function that needs it.

Never add a top-level `import fcntl` or `import msvcrt` outside this module.

## Key Rules

**Use `IS_WINDOWS`/`IS_POSIX` for all OS detection.** Not `sys.platform == "win32"`.
Inline `sys.platform` comparisons are a refactor trigger.

**Use `get_home_path()`, never `Path.home()`.** On Windows, `Path.home()` ignores
the `HOME` env var and queries Windows APIs, breaking test isolation.

**File locking (`locking.py`) has reentrancy semantics.** A thread that already holds
the lock re-enters safely. The OS lock releases only when the outermost `__exit__` runs.
Acquired handles are also tracked process-wide so a fork child closes every inherited
descriptor without explicitly unlocking the parent's open-file-description lock.
On Windows, the implementation writes a guard byte and fsyncs before the first lock
attempt — the file must be non-zero-length.

**Windows signal behavior diverges from POSIX in three ways that cause silent failures:**
- `os.kill(pid, SIGINT)` is unreliable — use `process.terminate()` instead.
- `loop.add_signal_handler()` is a no-op on ProactorEventLoop — use `signal.signal()` + `loop.call_soon_threadsafe()`.
- `os.kill(pid, SIGTERM)` maps to `TerminateProcess()` — unconditional kill, no handler runs.

## Process Scope (`process_scope/`)

`ScopedProcessHandle.terminate()` is the integration point. It dispatches to the
right backend based on `snapshot.containment`, is exception-safe, and handles logging.
Do not call `posix.terminate_pgid()` or `windows_job.terminate_job()` directly from
outside the package.

Capture `scope_snapshot` **before** calling `connection.stop()` — both values are
cleared inside `stop()`.

`terminate.py` is a backward-compat shim that delegates to `process_scope/fallback.py`.
Existing callers work unchanged; new code should use `ScopedProcessHandle`.

## Entry Points

- `__init__.py` — `IS_WINDOWS`, `IS_POSIX`, `get_home_path()`, deferred POSIX proxies
- `locking.py` — `lock_file()`, `try_lock_file()`: cross-platform exclusive file locking
- `process_scope/` — `ScopedProcessHandle`, `ProcessScopeSnapshot`, platform backends

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — import rules, locking reentrancy,
Windows signal exceptions, containment strategy and invariants

→ [process_scope/.context/CONTEXT.md](process_scope/.context/CONTEXT.md) — PID reuse
guard, POSIX group kill mechanics, Windows Job Object handle lifetime

## Related

- `../state/user_paths.py` — uses `get_home_path()` for state root resolution
- `../harness/claude.py` — uses `IS_WINDOWS` for signal routing (SIGINT vs terminate)
- `../state/atomic.py` — Windows vs POSIX fsync behavior
