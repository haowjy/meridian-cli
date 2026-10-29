# Cancel

`SpawnApplicationService.cancel` (`spawn_service.py`) is the only cancel. The CLI, the
Pi extension, `cancel_descendants` and `cancel-all` all call it. Design:
the spawn-identity-v2 design's "Cancel on the lock" (work item `spawn-identity-v2`).

## Sequence

1. **Observe once.** One stored-row read gives launch K (`row.launch_number`), the runner's `(pid, birth)`,
   `LaunchPaths.for_row(row)` and, for a managed primary, `primary_meta.json`. The stored row,
   not `read_derived`: a dead launch with no terminal yet *derives* as terminal, and the
   claim below must write it. A stored-terminal row returns at once. A live managed primary
   is refused without `--force`.
2. **Record the intent**: `cancel.json` in K's directory (first request wins), plus, only
   when the observed row has no `launch` (v3), the row's `cancel_intent` through the locked
   mutator, because an older-build runner decides `cancelled` only from that field. The
   mutator re-checks `launch` inside the write; a v4 row never gets the field. `read_cancel_intent` reads the file, else the
   field; the new runner, `derive` and the final fold all use it.
3. **Claim first.** `_finalize_dead_launch_unlocked(expected_n=K)` (the one dead-launch claim,
   shared with doctor and the repair pass, which pass no `expected_n`), non-blocking. Under
   the lock the claim checks the row is still launch K; if a continue moved it on, the claim
   releases and returns `Live` without writing, and step 4's `wait_until` finds K finished
   (see Rules). The teardown is its `before_finalize` (with `scopes_torn_down`, so
   `managed_scopes_pending` stays false). A
   launch without a runner is torn down (`state.managed_primary.tear_down_dead_launch`: a
   managed primary's backend and TUI, then every recorded scope, by exact birth, graces
   bounded by the time left before the deadline) and ends with no signal and no grace.
   `derive` decides the outcome: a durable report wins, with or without a cancel
   request, and makes it `succeeded`; otherwise the intent makes it `cancelled`. `Settled` returns the stored terminal.
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
- A cancel is pinned to K. Once the spawn has moved past K, `poll_launch` answers from
  `launches/K/launch.json` (written, with K's outcome, before K+1 starts), so the cancel
  returns K's outcome and never signals, sweeps or claims K+1: signals go to the exact
  `(pid, birth)` read in step 1. `CancelOutcome.launch_number` is K, and a result shown to
  the caller is consumed as `(spawn, K)`.
- Every runner's terminal write goes through `SpawnLifecycleService.finalize`, which defers
  SIGTERM and SIGHUP (`signal_coordinator().mask_termination()`: main thread only, re-delivered
  on unmask) for the length of the store write. A hangup can't turn a finished spawn into
  `crashed`, and `spawn.finished` hooks run unmasked. No runner masks by hand.
