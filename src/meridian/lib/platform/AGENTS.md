# lib/platform/

All platform-specific OS code lives here. Everything else in the codebase should
be platform-agnostic — if you're writing `sys.platform == "win32"` outside this
module, that's a refactor trigger.

Meridian is POSIX-first: Linux and macOS are supported. Existing native-Windows
branches (locking, process-scope, signals, path resolution) are untested,
best-effort legacy code; do not add or expand them.

## Mental Model

Two layers:

1. **Detection and primitives** (`__init__.py`, `locking.py`, `terminate.py`) —
   `IS_WINDOWS`, `IS_POSIX`, `get_home_path()`, file locking, process-tree teardown shim.

2. **Process containment** (`process_scope/`) — ensures the full subprocess tree
   Meridian starts is killed on spawn completion, cancel, or crash. Three backends:
   POSIX group kill, a legacy Windows Job Object branch, and a psutil fallback.

## Import Rule: Keep OS-Specific Imports in This Package

`fcntl`, `pty`, `termios`, `tty` are POSIX-only. `msvcrt` is Windows-only. Either:

- **Deferred proxy** (shared callers): `from meridian.lib.platform import fcntl, pty`
  — these are `DeferredUnixModule` instances that defer unsupported-module errors
  until first use; the legacy native-Windows behavior is untested.
- **Function-local import** (existing Windows-only path):
  `import msvcrt as _msvcrt` inside the function that needs it.

Never add a top-level `import fcntl` or `import msvcrt` outside this module.

## Key Rules

**Do not add or expand native-Windows branches.** Keep necessary OS detection
centralized here; when maintaining an existing branch, use
`IS_WINDOWS`/`IS_POSIX`, not inline `sys.platform` comparisons.

**Use `get_home_path()`, never `Path.home()`.** The helper honors `HOME`, which
keeps path resolution explicit and testable.

**File locking (`locking.py`) has reentrancy, shared mode, and fork-safety.**
`lock_file(path, mode="exclusive"|"shared", reentrant=True|False)` is the single
locking primitive. A thread that already holds the lock re-enters safely (when
reentrant). The OS lock releases only when the outermost `__exit__` runs. Acquired
handles are tracked process-wide so a fork child closes every inherited descriptor
without explicitly unlocking the parent's open-file-description lock. Shared mode
uses `LOCK_SH`; a held shared lock cannot be upgraded to exclusive in place. The
legacy Windows branch enforces exclusive locks but its shared mode is advisory-only;
POSIX enforces shared mode. Windows exclusive locking writes a guard byte and fsyncs
before the first lock attempt; this behavior is untested.

**Legacy Windows signal branches are untested.** Their intended behavior is:
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
- `locking.py` — `lock_file()`, `try_lock_file()`: advisory exclusive file locking
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
