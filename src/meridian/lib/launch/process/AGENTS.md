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
    ├── select_process_backend()   ← pick {native, PTY, subprocess, legacy Windows console}
    │
    ├── _execute_via_managed_attach()   ← PrimaryAttachLauncher path
    │       └── fallback on PrimaryAttachError → _execute_via_blackbox()
    │
    └── post-exit finalization
            ├── conclude_native_run() → identity, boundary, invocation attribution
            └── _finalize_lifecycle() → complete_execution()
```

**Backend selection:** `selector.py` consumes the resolved `TerminalSurfaceMode`.
Codex/OpenCode adapters default to native terminal inheritance; Claude/Pi retain
the PTY relay. Output capture uses PTY with pipe fallback. A TTY alone does not
choose the managed launch mode. Adapter-derived mode is never persisted/replayed
in launch-policy snapshots.

**Managed-attach fallback:** `PrimaryAttachError` follows the adapter's policy:
Codex raises, OpenCode falls back to black-box execution.

## Native-Primary Adapter Hooks

`run_harness_process()` stays harness-agnostic. Per-harness native-primary concerns
arrive through `SubprocessHarness` hooks — never `HarnessId` branches:

- `resolve_primary_command` / `redact_primary_command` — argv projection (e.g. Pi's
  resolved runtime path) and secret redaction before metadata persistence.
- `uses_native_primary_metadata` / `native_primary_runtime_metadata` — whether and
  which runtime fields populate `primary_meta.json`.
- `observe_after_exit` — exact entry validation, launch-correlated exit evidence,
  and diagnostic observations; the shared pipeline decides and persists.
- `native_observation_reader` — cursor-based harness decoding for the bounded
  per-run drainer; `NativeRun` owns ordering and state publication.
- `build_primary_runtime_request_handler` — managed-primary runtime request handler
  (Codex/OpenCode permission broker).
- `capabilities.captures_blackbox_output` and `bootstrap.primary_stderr_log` drive
  the black-box capture and stderr-log env, replacing harness-id conditionals.

Pi writes its `pi_runtime_meta.json` sidecar from `prepare_prelaunch`, so the primary
and spawn paths share one writer; `runner.py` never names a harness id.

## Hard Invariants

**I-4:** `conclude_native_run()` runs once after child exit, before lifecycle
completion. Managed attach routes owned IDs through `NativeRun.observe`; it does
not bind or decide identity independently.

**Assigned identity binds before exec.** When the finalized
`native_identity` carries an ID (Meridian-minted create, verified resume, or
fork target), the runner binds it as `source="assigned"` before starting the child;
a conflict refuses the launch. Only `NativeIdentity` values without an ID
(e.g. Claude fork) wait for the first owned observation. Observations bind once and never replace the key.
Adapter cleanup still runs on binding errors.

**Live observation is launcher-owned and lexical.** Each driver runs its child
inside `with native_run.observing(...)`; once `on_running` supplies the concrete
child PID, one bounded drainer starts. Leaving the block, normally or by
exception, final-drains and joins it before conclusion and session scope close.

**Session scope wraps everything.** `session_scope()` opens before the spawn row is
created and closes in the finally block. If `lifecycle_service.start()` fails, the
session is still properly closed.

**The TUI owns the terminal exclusively.** `terminal_ownership.py` duplicates the
terminal descriptors, redirects launcher fd 0 to `/dev/null` and fds 1 and 2 to
the run's dedicated `launcher.log`, and yields the duplicates before the TUI
process starts. Programs that open `/dev/tty` directly can still draw on the
screen. Both black-box and managed attach callers keep that window around start
callbacks and the relay through child exit; relays retain their own descriptor
duplicates so a timed-out daemon relay cannot write through a recycled fd. New
native-terminal drivers must enter the same window and explicitly attach the TUI
to its yielded fds. Terminal capability and `isatty` checks on fds 0/1/2 must run
before the window or use its saved fds. A background write racing restoration can
split across the descriptor swap; this is accepted because the TUI has already
exited. Only the post-exit result renderer may point users at captured launcher
output on a normal exit. On an escaping error, the ownership window emits the
same pointer after restoration, and only when the current window grew the log.

## Key Rules

**Don't add launcher types without registering in `select_process_backend()`.** The
selection logic reads `ProcessPlatformContract` — new launchers need entries there.

**Native job control belongs to the shell.** Launcher and TUI share its foreground
process group. Only during the TUI lifecycle does the launcher intercept SIGINT;
SIGTSTP is left alone. The native gate records no pgid and sends one root TERM on
runner death, never a shared-group signal or delayed KILL. The managed backend
uses its existing separate group gate.

## Entry Points

- `runner.py` — `run_harness_process()`. The sole external entry point.
- `selector.py` — terminal policy/capture → process backend.
- `native_launcher.py` — root-only gate, saved terminal fds, no inner PTY.
- `terminal_ownership.py` — fd-level exclusive terminal window shared by TUI drivers.
- `ports.py` — `ProcessLauncher` protocol, `ProcessBackendId`, backend selection types.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — backend selection rules in detail,
   managed-attach fallback conditions, finalization ownership, session ID observation.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — launch/ layer; three driving adapters;
  invariants I-1 through I-13.
- [../streaming/.context/CONTEXT.md](../streaming/.context/CONTEXT.md) — streaming spawn
  path; sibling to this package.
