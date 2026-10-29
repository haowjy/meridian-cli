# Cancel

`SpawnApplicationService.cancel` (`spawn_service.py`) is the only cancel. The CLI, the
Pi extension, `cancel_descendants` and `cancel-all` all call it. Design:
the spawn-identity-v2 design's "Cancel on the lock" (work item `spawn-identity-v2`).

## Sequence

1. **Observe once.** One stored-row read gives the runner's `(pid, birth)`,
   `LaunchPaths.for_row(row)` and, for a managed primary, `primary_meta.json`. The stored row,
   not `read_derived`: a dead launch with no terminal yet *derives* as terminal, and the
   claim below must write it. A stored-terminal row returns at once. A live managed primary
   is refused without `--force`.
2. **Record the intent**: `cancel.json` in the launch directory (first request wins), plus
   the row's `cancel_intent` through the locked mutator, because an older-build runner
   decides `cancelled` only from that field. `read_cancel_intent` reads the file, else the
   field; the new runner, `derive` and the final fold all use it.
3. **Claim first.** `claim_dead_launch`, non-blocking. A launch without a runner is swept
   (its recorded scopes; a managed primary's launcher, backend and TUI) and ends with no
   signal and no grace. The free lock plus the intent make it `cancelled`; report text
   does not (a durable report outranks the cancel only on the lockless path). `Settled`
   returns the stored terminal; `Lockless` (no lock file) uses the row's PID liveness.
4. **Signal and wait.** SIGTERM to the runner by exact birth; wait `_CANCEL_GRACE_SECS`;
   then SIGKILL the runner's tree and a managed primary's backend and TUI by exact birth;
   wait `_CANCEL_KILL_SECS`. Each wait is `state.spawn_status.wait_until`; when it sees the
   lock free it claims as in step 3. An unknown birth is never signalled.
5. **Deadline** (grace plus kill window, 6 s) with the lock still held: the outcome reports
   `finalizing`. The intent stays in the launch directory for whoever ends the launch.

## Rules

- Nothing here re-reads the row by ID after step 1 to decide what to kill or what to
  return; waits go through `poll_launch`.
- The runner masks SIGTERM and SIGHUP (`signal_coordinator().mask_termination()`) only while
  it writes its terminal, so a hangup can't turn a finished spawn into `crashed`.
