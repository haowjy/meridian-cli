# lib/platform/

Cross-platform OS primitives. All platform-specific code goes here — nowhere else.
Windows is a first-class product target; do not add inline `sys.platform` branches
outside this module.

## Entry Points

- `__init__.py` — `IS_WINDOWS`, `IS_POSIX`, `get_home_path()`, deferred POSIX module proxies
- `locking.py` — `lock_file()`, `try_lock_file()`: cross-platform exclusive file locking
- `terminate.py` — `terminate_tree()`, `terminate_tree_sync()`: process-tree teardown shim
- `unix_modules.py` — `DeferredUnixModule`, lazy proxies for `fcntl`, `pty`, `select`, `termios`, `tty`
- `windows_job.py` — Windows Job Object handle (used by `process_scope/windows_job.py`)
- `process_scope/` — platform-specific process containment (POSIX group, Windows Job Object, fallback)

## Depth

See [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Import rules for POSIX-only and Windows-only stdlib modules
- Locking reentrancy and Windows-specific guard byte requirement
- Process-scope containment strategy and teardown invariants
- Windows signal behavior exceptions (SIGINT unreliable, ProactorEventLoop limits)

## Related

- `../state/atomic.py` — `_fsync_directory`: Windows vs POSIX fsync behavior
- `../state/user_paths.py` — uses `get_home_path()` for state root resolution
- `../harness/claude.py` — uses `IS_WINDOWS` for signal routing (SIGINT vs terminate)
