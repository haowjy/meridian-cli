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
    ├── reserve_run()              ← exact continue only, before chat acquisition
    ├── session_scope()            ← acquire/open/close session generation
    ├── lifecycle_service.start()  ← fresh/fork row only; reopen already reserved
    ├── select_process_backend()   ← pick {PTY, subprocess, legacy Windows console}
    │
    ├── _execute_via_managed_attach()   ← PrimaryAttachLauncher path
    │       └── fallback on PrimaryAttachError → _execute_via_blackbox()
    │
    └── post-exit finalization
            ├── conclude_native_run() → identity, boundary, invocation attribution
            └── _finalize_lifecycle() → complete_execution()
```

**Backend selection rules:**
- Legacy native-Windows branch (untested): `WindowsConsoleLauncher`
- POSIX + TTY available: `PtyProcessLauncher`
- POSIX + no TTY (CI, piped): `SubprocessProcessLauncher`

**Managed-attach fallback:** if `PrimaryAttachLauncher` raises `PrimaryAttachError`
(harness didn't start the server), it falls back to `_execute_via_blackbox()`. This
is intentional — managed-primary is best-effort for the primary path.

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

**Reopen reserves before session scope.** Exact continue publishes a queued run
with no generation, then acquires the chat with try-lock and binds its generation.
Fresh/fork launches still create the row inside session scope. Failure to acquire
a live chat finalizes only the new run; the previous generation remains untouched.

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
