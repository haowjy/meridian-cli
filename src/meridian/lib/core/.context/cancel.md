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
3. **Claim first.** `_finalize_dead_launch_unlocked` (the one dead-launch claim, shared with
   doctor and the repair pass), non-blocking, with the teardown as its `before_finalize`. A
   launch without a runner is torn down (`state.managed_primary.tear_down_dead_launch`: a
   managed primary's backend and TUI, then every recorded scope, by exact birth, graces
   bounded by the time left before the deadline) and ends with no signal and no grace.
   `derive` decides the outcome: the intent makes it `cancelled` unless the launch left a
   durable report, which wins and makes it `succeeded`. `Settled` returns the stored terminal.
   `Lockless` (no lock file) is cancel's only extra branch: it runs the same teardown when
   `legacy_runner_alive` says the runner is gone, then writes `derive(row)`.
4. **Signal and wait.** SIGTERM to the runner by exact birth; wait `_CANCEL_GRACE_SECS`;
   then SIGKILL the runner's tree and a managed primary's backend and TUI by exact birth;
   wait `_CANCEL_KILL_SECS`. Each wait is `state.spawn_status.wait_until`; when it sees the
   lock free it claims as in step 3. An unknown birth is never signalled.
5. **Deadline** (grace plus kill window, 6 s) with the lock still held: the outcome has
   status `finalizing` and exit code 1 (`cancel-all` counts it as finalizing). The intent
   stays in the launch directory for whoever ends the launch.

## Rules

- Nothing here re-reads the row by ID after step 1: the deadline outcome, the lockless
  branch and the managed-primary refusal all use the row and `primary_meta.json` read in
  step 1. Waits go through `poll_launch(spawn, n)`, whose `LockFree` carries the derived row.
- Every runner's terminal write goes through `SpawnApplicationService._complete_spawn_unlocked`,
  which masks SIGTERM and SIGHUP (`signal_coordinator().mask_termination()`) for the length
  of the write, so a hangup can't turn a finished spawn into `crashed`. No runner masks by hand.
