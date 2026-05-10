# launch/process/

Process execution layer. Owns the primary harness process lifecycle: launcher
selection, session tracking, process execution, managed-attach flow, and
post-exit finalization.

## Entry Points

- `runner.py` — `run_harness_process()`: start session, create spawn row, run process, finalize
- `ports.py` — `ProcessLauncher` protocol, `ProcessBackendId`, `ProcessSurfaceMode`,
  `ProcessPlatformContract`, `SelectedProcessLauncher`

## Key Files

- `runner.py` — `select_process_backend()`: picks launcher from {PTY, subprocess, Windows console}
- `primary_attach.py` — `PrimaryAttachLauncher`: managed backend + TUI attach flow
- `pty_launcher.py` — `PtyProcessLauncher`: PTY-based process launch (POSIX)
- `subprocess_launcher.py` — `SubprocessProcessLauncher`: subprocess-based launch (cross-platform)
- `windows_launcher.py` — `WindowsConsoleLauncher`: Windows console launcher
- `session.py` — `build_session_metadata()`, `resolve_attached_work_id()`, `resolve_primary_session_mode()`

## Architecture

```
run_harness_process()
    │
    ├── session_scope()          ← start/stop session store entry
    ├── lifecycle_service.start()← create spawn row
    ├── select_process_backend() ← pick PTY / subprocess / Windows
    │
    ├── _execute_via_managed_attach()  ← PrimaryAttachLauncher path
    │       └── fallback on PrimaryAttachError → _execute_via_blackbox()
    │
    └── _finalize_lifecycle_and_observe_session()
            ├── spawn_service.complete_execution()
            └── harness_adapter.observe_session_id()  ← I-4: called once post-exit
```

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — backend selection rules, managed-attach fallback,
  session ID observation invariant, finalization ownership

## Related

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — launch/ layer; four driving adapters; invariants
→ [../streaming/.context/CONTEXT.md](../streaming/.context/CONTEXT.md) — streaming spawn path (sibling)
→ [KB: architecture/launch-system.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/launch-system.md)
→ [KB: architecture/managed-primary-lifecycle.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/managed-primary-lifecycle.md)
