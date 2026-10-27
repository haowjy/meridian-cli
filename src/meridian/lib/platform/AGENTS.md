# lib/platform/

All OS-specific code lives here; elsewhere, `sys.platform == "win32"` is a refactor
trigger. Meridian is POSIX-first (Linux, macOS). Existing native-Windows branches (locking,
process-scope, signals, paths) are untested legacy: don't add or expand them, and use
`IS_WINDOWS`/`IS_POSIX` rather than inline `sys.platform`.

Layers: detection and primitives (`__init__.py`, `locking.py`, `terminate.py`,
`launch_gate.py`), and process containment (`process_scope/`).

## Rules

- **Keep OS-specific imports here.** `fcntl`, `pty`, `termios`, `tty` (POSIX) and `msvcrt`
  (Windows) are imported elsewhere only through the deferred proxies
  (`from meridian.lib.platform import fcntl, pty`) or a function-local import; never a
  top-level `import fcntl`/`import msvcrt` outside this package.
- **`get_home_path()`, never `Path.home()`**; it honors `HOME`.
- **`lock_file(path, mode="exclusive"|"shared", reentrant=...)` is the single locking
  primitive.** A reentrant thread re-enters safely and the OS lock releases at the outermost
  exit. A shared lock cannot be upgraded in place. Fork children close inherited descriptors
  without unlocking the parent's open-file-description lock. `read_only_existing=True` is
  reserved for the project-lifetime lock projected read-only into harness sandboxes.
- **Stable lock inodes and lock order**: unlink only via `unlink_validated_lock()`, and
  follow the cross-store order, both stated once in
  [../state/AGENTS.md](../state/AGENTS.md).
- `terminate.py` is a shim over `process_scope/fallback.py`; new code uses
  `ScopedProcessHandle`.
- `launch_gate.py` is the pre-exec identity gate and EOF-driven guardian; close means runner
  death or kill.

[.context/CONTEXT.md](.context/CONTEXT.md) — import rules, locking, Windows exceptions ·
[process_scope/AGENTS.md](process_scope/AGENTS.md)
