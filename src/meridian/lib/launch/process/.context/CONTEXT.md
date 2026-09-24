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

`harness_adapter.observe_session_id()` is called **exactly once** after the process exits,
inside `_finalize_lifecycle_and_observe_session()`. It discovers the harness session ID from
artifacts (history.jsonl, output.jsonl) written during execution.

Generated seeds and native-fork source IDs are not authoritative child identities.
Fresh/native-fork model selections stay pending until an observed ID binds the captured
startup attempt. Exact resume and materialized forks already have known identities.
The existing Claude transcript/trampoline detector can confirm a generated seed or
its successor; a seed alone cannot. No last-executed model is inferred.

Observation failures remain best-effort. Persisting an observed identity is required:
errors propagate, and a conflicting known identity is rejected. Adapter cleanup still
runs. Do not call `observe_session_id()` elsewhere in the lifecycle.

## Finalization Ownership

`_finalize_lifecycle_and_observe_session()` is called in a `finally` block inside
`run_harness_process()`. It is responsible for:
- Calling `spawn_service.complete_execution()` with `ExecutionTerminalFacts`
- Resolving the final exit code (may differ from process exit code for graceful report-completion)
- Persisting observed harness session ID

The surrounding `finally` calls `harness_adapter.cleanup_prelaunch()`, including when
identity persistence fails.

`complete_execution()` is idempotent — safe to call on a spawn already in terminal state.

## run_harness_process() Caller Contract

Callers provide a preview `LaunchContext` with a valid `binding.argv`; optional prepared
content is not an authorization token. Before `session_scope`, the runner reconciles the
context's request copies, adapter, runtime namespace and executable selector with every
supplied prepared copy, then revalidates source use. Legacy callers without prepared
content are composed at this point. The function then:
1. Opens `session_scope` (creates session store entry)
2. Calls `lifecycle_service.start()` (creates spawn row, sets status to `queued`)
3. Materializes fork if `session_mode == FORK` and harness supports it
4. Privately binds `PreparedLaunchSurface` with real spawn ID and paths (also for legacy callers)
5. Calls `harness_adapter.prepare_prelaunch()` — env overrides applied to `child_env`
6. Executes process
7. Finalizes lifecycle in `finally`

The `prepared` argument carries a `PreparedLaunchSurface` from the prepare/bind split. Binding
is private after the row exists, so this runner boundary does not repeat the public source query
or reinterpret an already checked selector as a new source.

## Lateral Links

→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — composition seam, three driving adapters, invariants I-1/I-4/I-10
→ [../../streaming/.context/CONTEXT.md](../../streaming/.context/CONTEXT.md) — streaming spawn execution (sibling path)
