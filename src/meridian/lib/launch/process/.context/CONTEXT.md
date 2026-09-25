# launch/process — Contracts and Architecture

## Backend Selection

`select_process_backend()` returns a `SelectedProcessLauncher` with an explicit
`ProcessPlatformContract` describing how IO is surfaced.

Selection order when output capture is requested (`output_log_path` is set):
1. `PtyProcessLauncher` (PTY_MEDIATED, captures to artifact) — if `can_use_pty()`
2. `SubprocessProcessLauncher` (PIPE_CAPTURE, captures to artifact) — fallback

Selection order when no capture is needed (`output_log_path` is None):
1. `WindowsConsoleLauncher` (NATIVE_INHERIT, no capture) — legacy, untested;
   selected if `can_use_windows_console_launcher()`
2. `PtyProcessLauncher` (PTY_MEDIATED, no capture) — elif `can_use_pty()`
3. `SubprocessProcessLauncher` (NATIVE_INHERIT, no capture) — fallback

The PTY launcher is preferred for POSIX interactive TUI harnesses. The legacy
Windows console launcher attempts native console inheritance; the subprocess
launcher is the fallback.

`captures_output_to_artifact` on `ProcessPlatformContract` is authoritative — callers must
not assume capture based on launcher type alone.

`ProcessLauncher.start()` returns a `RunningProcess` immediately after process birth.
PID/scope recording happens before `RunningProcess.wait()` begins the blocking terminal
relay. Keep those phases separate: managed-primary cancellation depends on having a
concrete process identity before it can wait for exit or terminate the scope.
`RunningProcess.cancel_wait()` must release its caller-facing wait even when scope
termination cannot confirm child exit. Managed attach also directly terminates the
concrete process as a fallback and bridges the synchronous wait through a daemon thread,
so an uncooperative relay cannot be re-joined by `asyncio.run()` during loop shutdown.

## Managed Attach vs Black-Box

`_execute_primary_process()` checks `harness_contract.bootstrap.mode`. When mode is
`managed_primary_attach`:

1. Calls `run_primary_attach()` → `PrimaryAttachLauncher`
2. On `PrimaryAttachError`: if `primary_attach_failure_policy == "raise"`, re-raises; otherwise
   logs a warning, deletes managed sidecars (`PRIMARY_META_FILENAME`, `OUTPUT_FILENAME`,
   `stderr.log`), and falls back to black-box
3. Black-box: calls `run_primary_process_with_capture_fn()` directly

Managed attach persists the harness session ID from `PrimaryAttachOutcome.session_id`
immediately on success. Black-box path may discover the session ID only at exit via
`observe_session_id()`.

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
- The relay returns exit 130 when `cancel_wait()` unblocks it; cancellation is asserted
  only when the signal was seen **and** the relay returned 130, so a session that exited
  normally just before the signal is not recorded as cancelled.
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

`harness_adapter.observe_session_id()` is called once after captured execution,
before lifecycle completion. The first owned event is compared with the assigned
key; a contradiction fails as `entry_mismatch` with expected/observed evidence.
Managed attach validates its initial connection identity before attaching.

Assigned keys bind before exec. Observed-only plans bind from owned signals;
neither path discovers a replacement. Post-exit native verification precedes
diagnostic observation, boundary finalization, and invocation attribution.
Claude fullscreen candidates stay diagnostic, never verified exit identities.

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

The `prepared` argument carries a `PreparedLaunchSurface` from the prepare/bind split. When
present, `bind_launch_context()` is used instead of `build_launch_context()` — this is the
primary CLI's prepare-once/bind-twice optimization path.

## Lateral Links

→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — composition seam, three driving adapters, invariants I-1/I-4/I-10
→ [../../streaming/.context/CONTEXT.md](../../streaming/.context/CONTEXT.md) — streaming spawn execution (sibling path)
