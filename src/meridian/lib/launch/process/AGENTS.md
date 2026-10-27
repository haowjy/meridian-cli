# launch/process/ — Primary Process Executor

Owns the primary harness process lifecycle: launcher selection, session tracking,
process execution, managed-attach flow, post-exit finalization. This is driving adapter
#1 (Primary CLI) from `launch/__init__.py`.

`run_harness_process()` in `runner.py` is the sole external entry point:
`session_scope()` → `lifecycle_service.start()` (spawn row) → `select_process_launcher()`
→ managed attach (falls back to black-box on `PrimaryAttachError` when the adapter's
policy says so: Codex raises, OpenCode falls back) → `conclude_native_run()` →
`_finalize_lifecycle()`.

`selector.py` maps the resolved `TerminalSurfaceMode` to a launcher. Codex/OpenCode
default to native terminal inheritance; Claude/Pi keep the PTY relay. A TTY alone does
not pick the managed mode, and adapter-derived mode is never persisted in launch-policy
snapshots. Register any new launcher in `select_process_launcher()`.

## Native-Primary Adapter Hooks

`runner.py` stays harness-agnostic: per-harness concerns arrive through
`SubprocessHarness` hooks (`resolve_primary_command`, `redact_primary_command`,
`uses_native_primary_metadata`, `native_primary_runtime_metadata`, `observe_after_exit`,
`native_observation_reader`, `build_primary_runtime_request_handler`,
`capabilities.captures_blackbox_output`, `bootstrap.primary_stderr_log`), never
`HarnessId` branches. Pi's `pi_runtime_meta.json` sidecar is written from
`prepare_prelaunch`, so primary and spawn share one writer.

## Rules

- **`conclude_native_run()` runs once** after child exit, before lifecycle completion.
  Managed attach routes owned IDs through `NativeRun.observe`; it does not bind or
  decide identity itself.
- **Assigned identity binds before exec.** A finalized `native_identity` with an ID
  (minted create, verified resume, fork target) binds as `source="assigned"` before the
  child starts; a conflict refuses the launch. Identities without an ID (Claude fork)
  wait for the first owned observation. Observations bind once and never replace the key.
- **Observation is lexical.** Each driver runs its child inside
  `with native_run.observing(...)`; leaving the block final-drains and joins the drainer
  before conclusion and session-scope close.
- **`session_scope()` opens before the spawn row and closes in `finally`**, even if
  `lifecycle_service.start()` fails.
- **The TUI owns the terminal exclusively** (`terminal_ownership.py` dups the terminal fds,
  redirects 0/1/2 away, yields the dups). New native-terminal drivers must enter that
  window and attach the TUI to its yielded fds; run `isatty` checks before it. Detail:
  [.context/CONTEXT.md](.context/CONTEXT.md).
- **Native job control belongs to the shell.** Drivers enter `native_sigint_shield()`;
  SIGTSTP is left alone; the native gate sends one root TERM, never a group signal.
- **Signal receivers only latch.** Every primary path registers a SIGTERM/SIGHUP
  `SignalCallbackReceiver` with `escalate_on_repeat`, reports `cancelled`, exits 130. The
  callback runs between bytecodes, so no non-reentrant lock: no `LaunchGate` call, no
  `Event.set()`. Latch a bool and act from a main-thread poll. Dead-tty cleanup catches
  `OSError` **and** `termios.error`. See [.context/CONTEXT.md](.context/CONTEXT.md).

## Depth

[.context/CONTEXT.md](.context/CONTEXT.md) — backend selection, managed-attach fallback,
finalization, session ID observation, terminal ownership, signal cancellation.
Parent: [../AGENTS.md](../AGENTS.md); sibling spawn path: [../streaming/AGENTS.md](../streaming/AGENTS.md).
