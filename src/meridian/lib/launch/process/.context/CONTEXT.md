# launch/process — Contracts and Architecture

## Backend Selection

`select_process_launcher()` returns the launcher directly from the adapter's
`terminal_surface_mode` and the output-capture requirement.

Selection order when output capture is requested (`output_log_path` is set):
1. `PtyProcessLauncher` (PTY_MEDIATED, captures to artifact) — if `can_use_pty()`
2. `SubprocessProcessLauncher` (PIPE_CAPTURE, captures to artifact) — fallback

Without capture, the resolved adapter mode chooses native inheritance or the PTY
relay (with the existing unavailable-PTY/legacy Windows fallbacks). Native mode
does not test `isatty`: the native TUI is always root-only, even without TTY
stdin. It preserves the shell's session/group and uses `LaunchGate(group=False)`.
Its saved terminal descriptors come from the enclosing
`tui_terminal_window()`, not redirected launcher fds 0/1/2.

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

Native cancellation before wait returns 130 without opening or closing the gate:
the stub cannot exec, and the lifecycle owner terminates the recorded scope.
Native `terminate()` releases after polling/reaping: an exited root disarms the
guardian, an unreaped root closes the pipe as the fail-closed fallback. Runner
death closes the pipe regardless of cancellation. The root-only guardian sends
one TERM, never a shared-group signal or delayed KILL. Relay guardians use the
TUI teardown grace for group termination.

## Managed Attach vs Black-Box

An optional harness idle sensor runs as a task in the managed-attach event loop.
It starts only after the TUI is running and is cancelled before the live
connection stops; the black-box fallback never starts it.

`_execute_primary_process()` checks `harness_contract.bootstrap.mode`. When mode is
`managed_primary_attach`:

1. Calls `run_primary_attach()` → `PrimaryAttachLauncher`
2. On `PrimaryAttachError`: if `primary_attach_failure_policy == "raise"`, re-raises; otherwise
   logs a warning, deletes managed sidecars (`LaunchPaths.primary_meta`, `.output`,
   `.stderr_log`), and falls back to black-box
3. Black-box: calls `run_primary_process_with_capture_fn()` directly

Managed attach feeds owned IDs into `NativeRun.observe`. Black-box artifact IDs
enter the same pipeline through `conclude_native_run` after exit.

### Signal cancellation

Both primary paths register a `SignalCallbackReceiver` for `SIGTERM`/`SIGHUP` with the
process-global `SignalCoordinator` (`lib/launch/signals.py`): `PrimaryAttachLauncher` for
all of `run()`, and black-box `run_primary_process_with_capture()` from before child
start through the wait. A closed terminal sends SIGHUP and a killed launcher (pane kill,
`systemctl stop` of its scope) sends SIGTERM. Catching them lets the launcher stop the
TUI and finalize normally instead of dying at the default disposition with no runner
exit and no session stop (#623). Both paths report the latch as
`PrimaryExecutionResult.cancelled`, which feeds `cancellation_observed`, and exit 130.

**Receivers run in handler context.** Python runs the callback on the main thread
between bytecodes. A callback that takes a non-reentrant lock the main thread already
holds deadlocks the launcher against itself. `LaunchGate` (`terminate()`/`close()`) and
`threading.Event.set()` both take one, and PTY `cancel_wait()` is an `Event.set()`. So
the black-box callback only latches a bool: `running.wait()` runs on a daemon thread,
and the main thread polls it every 100 ms and calls `terminate()` then `cancel_wait()`
once, outside handler context. Managed attach calls `cancel_wait()` straight from its
handler; that is safe only because its launcher is native, whose `cancel_wait()` sets a
plain attribute. A new receiver that needs the gate or an Event uses the latch + poll
shape.

- `SignalCoordinator` installs handlers for the union of its active receivers' target
  signals (main thread only) and restores any signal no longer targeted; it is the single
  seam for the `SignalCallbackReceiver`s of both launchers.
- Managed attach: a SIGTERM/SIGHUP latched when the main task resumes from the TUI
  wait yields cancelled/130 even if the TUI exits zero. Black-box: a signal latched
  before the wait thread reports completion does the same. Later signals do not change
  the result; a signal concurrent with exit may be classified either way (accepted).
- Both receivers use `escalate_on_repeat` with a grace window: the first signal requests
  cancellation; a repeat restores `SIG_DFL` and re-raises only when it arrives at least
  `escalate_repeat_grace_secs` (default 1s) after the first signal of that number. A
  terminal close delivers SIGHUP **twice** (~1 ms apart), often with a TERM; escalating
  inside that burst would kill the launcher before finalize, so a burst routes to the
  (idempotent) callback while a deliberate later repeat still force-quits a wedged wait.
- Both receivers are unregistered **before** `_finalize_lifecycle()`. A TERM/HUP during
  finalize still kills the launcher at the default disposition, and the run stays
  active (the #623 bug in a smaller window). Both paths also replace the child's raw
  exit code with 130 at the process layer. Identity-v2 PR 4 owns both seams: hold a
  latch-only receiver across the sole outcome write, and carry raw code + `cancelled`
  to the writer, mapping to 130 there.
- Both TUI drivers enter `native_sigint_shield()` on the main thread alongside
  `tui_terminal_window()`. Only native launchers register the SIGINT receiver;
  interception ends before backend teardown. Backend-startup Ctrl+C propagates.
  The receiver is a Python handler, not SIG_IGN, so the child exec resets SIGINT.
- Black-box poll loop: any `BaseException` on the main thread (a `KeyboardInterrupt`
  from an unshielded SIGINT in PTY mode) also terminates and cancels once, joins the
  wait thread with a bound, and re-raises. Without that, the daemon relay keeps the
  outer tty raw, the spawn records failed, and the harness outlives finalize.
- Black-box stops the child only through the gate, never `os.kill`. PTY
  (`LaunchGate(group=True)`): the guardian TERMs the group, then KILLs after the grace.
  Native (`LaunchGate(group=False)`): `terminate()` releases the root-only gate, so the
  guardian sends one TERM and nothing escalates (#657). A signal before `start()` still
  launches the harness and cancels it within one poll (accepted).
- Managed attach, native `cancel_wait()` only sets a signal-safe flag to unblock the
  waiter; it neither terminates the child nor releases the gate. On cancel the
  lifecycle owner sends the single TERM via the recorded, birth-checked tree teardown,
  escalating to birth-checked KILL after its grace if needed. `terminate()` then
  reaps/disarms the guardian; normal reaped exit also disarms it. This owner
  teardown is distinct from the root-only guardian's runner-death behavior.
  Harness exit text is never scrubbed; Meridian's hint is appended after the
  terminal window closes.
- A signal that arrives before the TUI exists (during backend startup) is latched and
  honored once the TUI starts. If startup then fails, `run()` returns a cancelled outcome
  rather than raising, so `_execute_primary_process()` does not fall back to a black-box
  TUI that would ignore the termination request.
- `_copy_primary_pty_output` treats a lost pane as a clean stop. After SIGHUP the outer
  tty may already be gone, so raw-mode setup, termios restore, and the cleanup escape
  writes suppress both `OSError` and `termios.error`: **`termios.error` is not an
  `OSError` subclass**, and catching `OSError` alone left a traceback on pane kill.
  `OSError` (EIO) on the stdout write, the stdin read, or the master write ends
  forwarding rather than escaping the relay thread. A `select` `OSError` drops stdin
  and keeps draining the master; breaking out would leave the child blocked on a full
  pty while `waitpid` waits for it. A set `wait_cancelled` returns 130 before the
  blocking `waitpid` so a closed terminal cannot wedge the relay.

**Live-probe reach.** The CLI cannot select native black-box: Claude is always PTY,
Codex raises on attach failure, and OpenCode reaches it only through the managed-attach
fallback. Live TERM/HUP/pane-kill/scope-stop probes therefore cover PTY black-box and
native managed attach; native black-box is covered only by the fake-launcher seam test
(`tests/integration/launch/test_blackbox_primary_signals.py`).

## Session ID Observation

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
2. Calls `lifecycle_service.start()` (creates the spawn row, `running`)
3. Materializes fork if `session_mode == FORK` and harness supports it
4. Rebuilds `LaunchContext` with real spawn ID and paths (or binds from `PreparedLaunchSurface`)
5. Calls `harness_adapter.prepare_prelaunch()` — env overrides applied to `child_env`
6. Executes process
7. Finalizes lifecycle in `finally`

The mandatory `prepared` argument carries a `PreparedLaunchSurface` from the
prepare/bind pipeline; `run_harness_process()` binds it again with the real spawn
ID and paths after the primary CLI's preview bind.

## Lateral Links

→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — composition seam, three driving adapters, rules
→ [../../streaming/.context/CONTEXT.md](../../streaming/.context/CONTEXT.md) — streaming spawn execution (sibling path)

## Terminal ownership (from AGENTS.md)

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

## Native job control (from AGENTS.md)

**Native job control belongs to the shell.** Launcher and TUI share its foreground
process group. Both drivers enter `native_sigint_shield()` on the main thread alongside the
terminal window, intercepting SIGINT only during the native TUI lifecycle;
SIGTSTP is left alone. The native gate records no pgid and sends one root TERM on
runner death, never a shared-group signal or delayed KILL. The managed backend
uses its existing separate group gate.
