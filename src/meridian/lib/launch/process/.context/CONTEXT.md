# launch/process — Contracts and Architecture

## Backend Selection

`select_process_backend()` returns a `SelectedProcessLauncher` with an explicit
`ProcessPlatformContract` describing how IO is surfaced.

Selection order when output capture is requested (`output_log_path` is set):
1. `PtyProcessLauncher` (PTY_MEDIATED, captures to artifact) — if `can_use_pty()`
2. `SubprocessProcessLauncher` (PIPE_CAPTURE, captures to artifact) — fallback

Without capture, the resolved adapter mode chooses native inheritance or the PTY
relay (with the existing unavailable-PTY/legacy Windows fallbacks). Native mode
does not test `isatty`: it always preserves the shell's session/group and uses
`LaunchGate(group=False)`. Its saved terminal descriptors come from the enclosing
`tui_terminal_window()`, not redirected launcher fds 0/1/2.

`captures_output_to_artifact` on `ProcessPlatformContract` is authoritative — callers must
not assume capture based on launcher type alone.

`ProcessLauncher.start()` returns a `RunningProcess` immediately after process birth.
`LaunchGate.adopt()` captures its `ProcessIdentity`: observed birth and intended
group, not a racy post-fork `getpgid` read. Scope persistence finishes before
`RunningProcess.wait()` opens the gate and begins the blocking terminal relay.
No-capture subprocesses inherit the runner group only with TTY stdin; their scope
has no pgid, protecting other shell pipeline members.

Keep those phases separate: managed-primary cancellation depends on having a
concrete process identity before it can wait for exit or terminate the scope.
`RunningProcess.cancel_wait()` must release its caller-facing wait even when scope
termination cannot confirm child exit. Managed attach also closes the gate as a
fallback and bridges the synchronous wait through a daemon thread,
so an uncooperative relay cannot be re-joined by `asyncio.run()` during loop shutdown.


Black-box execution requires a durable start callback. Managed attach alone
records the TUI's session-owned scope; its runner callback only observes/marks
running. Captured print runs use `stdio` / `harness_stdio`, not TUI labels.

Cancel before wait closes the gate and reaps the stub's exit 125, without exec.
`terminate()` closes the gate: it must not send an additional root TERM. After
relay EOF, a known reaped returncode disarms the guardian; an interrupted wait
closes it as runner death. The TUI teardown grace also configures the guardian.

## Managed Attach vs Black-Box

An optional harness idle sensor runs as a task in the managed-attach event loop.
It starts only after the TUI is running and is cancelled before the live
connection stops; the black-box fallback never starts it.

`_execute_primary_process()` checks `harness_contract.bootstrap.mode`. When mode is
`managed_primary_attach`:

1. Calls `run_primary_attach()` → `PrimaryAttachLauncher`
2. On `PrimaryAttachError`: if `primary_attach_failure_policy == "raise"`, re-raises; otherwise
   logs a warning, deletes managed sidecars (`PRIMARY_META_FILENAME`, `OUTPUT_FILENAME`,
   `stderr.log`), and falls back to black-box
3. Black-box: calls `run_primary_process_with_capture_fn()` directly

Managed attach feeds owned IDs into `NativeRun.observe`. Black-box artifact IDs
enter the same pipeline through `conclude_native_run` after exit.

### Signal cancellation

`PrimaryAttachLauncher` registers a `SignalCallbackReceiver` (targets `SIGTERM`/`SIGHUP`)
with the process-global `SignalCoordinator` (`lib/launch/signals.py`) for the duration of
`run()`. A closed terminal sends SIGHUP and a killed process sends SIGTERM; catching them
lets the launcher stop the TUI relay through `RunningProcess.cancel_wait()` and finalize
normally, instead of dying and leaving an active record for orphan reconciliation. The
receiver is unregistered in `run()`'s `finally`, which restores the previous handlers.

- `SignalCoordinator` installs handlers for the union of its active receivers' target
  signals (main thread only) and restores any signal no longer targeted; it is the single
  seam for both the streaming `SignalForwarder` (`SIGINT`/`SIGTERM` → subprocess) and this
  launcher receiver.
- Cancellation is decided by observation order: a SIGTERM/SIGHUP before the wait
  thread observes TUI completion yields cancelled/130 even if the TUI exits zero.
  Completion observed first preserves the real result. The ordering lock must be
  reentrant because coordinator callbacks run in Python signal handlers.
- SIGINT interception starts immediately before TUI startup, after telemetry has
  cleared, and ends before backend teardown. Startup Ctrl+C still propagates.
- Native `cancel_wait()` only sets a signal-safe flag to unblock the waiter; it
  does not terminate the child. Wait release closes an unreaped gate, while the
  lifecycle owner tears down the recorded scope and reaps the root. Normal reaped
  exit disarms the guardian. Harness exit text is never scrubbed; Meridian's hint
  is appended after the terminal window closes.
- A signal that arrives before the TUI exists (during backend startup) is latched and
  honored once the TUI starts. If startup then fails, `run()` returns a cancelled outcome
  rather than raising, so `_execute_primary_process()` does not fall back to a black-box
  TUI that would ignore the termination request.
- The receiver uses `escalate_on_repeat` with a grace window: the first signal requests
  cancellation; a repeat restores `SIG_DFL` and re-raises only when it arrives at least
  `escalate_repeat_grace_secs` (default 1s) after the first signal of that number. A
  terminal close delivers SIGHUP **twice** (~1 ms apart); escalating on the second would
  kill the launcher before finalize, so a burst inside the grace routes to the (idempotent)
  callback while a deliberate later repeat still force-quits a wedged teardown.
- `_copy_primary_pty_output` treats a lost pane as a clean stop: `OSError` (EIO) on the
  stdout write, the stdin read, or the master write ends forwarding rather than escaping
  the relay thread, and a set `wait_cancelled` returns 130 before the blocking `waitpid`
  so a closed terminal cannot wedge the relay on a TUI in its own pty session.

## Session ID Observation — Invariant I-4

`bind_entry` owns pre-exec assignment. `NativeRun.observe` checks only the first
owned signal against pre-exec facts. `conclude_native_run` then observes artifact
and connection IDs, invokes `observe_after_exit`, verifies entry evidence and
allocates an exact exit chat before writing the boundary and attributing startup.
Claude fullscreen successors remain diagnostic, never verified exit identities.

## Finalization Ownership

`_finalize_lifecycle()` completes execution only after identity validation.
Typed identity errors prevent durable-report success and attribution.
The surrounding `finally` always calls adapter prelaunch cleanup.
`complete_execution()` is idempotent.

## run_harness_process() Caller Contract

Callers must provide a fully resolved `LaunchContext` with a valid `binding.argv`. The function:
1. Opens `session_scope` (creates session store entry)
2. Calls `lifecycle_service.start()` (creates spawn row, sets status to `queued`)
3. Materializes fork if `session_mode == FORK` and harness supports it
4. Rebuilds `LaunchContext` with real spawn ID and paths (or binds from `PreparedLaunchSurface`)
5. Calls `harness_adapter.prepare_prelaunch()` — env overrides applied to `child_env`
6. Executes process
7. Finalizes lifecycle in `finally`

The mandatory `prepared` argument carries a `PreparedLaunchSurface` from the
prepare/bind pipeline; `run_harness_process()` binds it again with the real spawn
ID and paths after the primary CLI's preview bind.

## Lateral Links

→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — composition seam, three driving adapters, invariants I-1/I-4/I-10
→ [../../streaming/.context/CONTEXT.md](../../streaming/.context/CONTEXT.md) — streaming spawn execution (sibling path)
