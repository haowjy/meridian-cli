# launch/process/ — Primary Process Executor

Owns the primary harness process lifecycle: launcher selection, session tracking,
process execution, managed-attach flow, and post-exit finalization. This is the
implementation of driving adapter #1 (Primary CLI) from `launch/__init__.py`.

## Mental Model

`run_harness_process()` is the entry point. It orchestrates a sequence with clear
ownership at each step:

```
run_harness_process()
    │
    ├── session_scope()            ← open/close session store entry
    ├── lifecycle_service.start()  ← create spawn row
    ├── select_process_backend()   ← pick {PTY, subprocess, legacy Windows console}
    │
    ├── _execute_via_managed_attach()   ← PrimaryAttachLauncher path
    │       └── fallback on PrimaryAttachError → _execute_via_blackbox()
    │
    └── _finalize_lifecycle_and_observe_session()
            ├── spawn_service.complete_execution()
            └── harness_adapter.observe_session_id()  ← I-4: called exactly once
```

**Backend selection rules:**
- Legacy native-Windows branch (untested): `WindowsConsoleLauncher`
- POSIX + TTY available: `PtyProcessLauncher`
- POSIX + no TTY (CI, piped): `SubprocessProcessLauncher`

**Managed-attach fallback:** if `PrimaryAttachLauncher` raises `PrimaryAttachError`
(harness didn't start the server), it falls back to `_execute_via_blackbox()`. This
is intentional — managed-primary is best-effort for the primary path.

## Hard Invariants

**I-4:** `harness_adapter.observe_session_id()` is called exactly once per launch,
in `_finalize_lifecycle_and_observe_session()`, after the process exits. Never call
it during execution, and never call it twice.

**Session scope wraps everything.** `session_scope()` opens before the spawn row is
created and closes in the finally block. If `lifecycle_service.start()` fails, the
session is still properly closed.

## Key Rules

**Don't add launcher types without registering in `select_process_backend()`.** The
selection logic reads `ProcessPlatformContract` — new launchers need entries there.

**Managed-attach fallback is silent.** The user sees the primary TUI either way —
the distinction is whether Meridian controls the turn or just observes.

## Entry Points

- `runner.py` — `run_harness_process()`. The sole external entry point.
- `ports.py` — `ProcessLauncher` protocol, `ProcessBackendId`, backend selection types.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — backend selection rules in detail,
   managed-attach fallback conditions, finalization ownership, session ID observation.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — launch/ layer; three driving adapters;
  invariants I-1 through I-13.
- [../streaming/.context/CONTEXT.md](../streaming/.context/CONTEXT.md) — streaming spawn
  path; sibling to this package.
