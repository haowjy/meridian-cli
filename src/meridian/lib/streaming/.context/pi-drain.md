# lib/streaming/ — Pi Drain Context

Pi-specific quiescence and tracked-child behavior for spawned Pi sessions. The
generic streaming runtime remains in [CONTEXT.md](CONTEXT.md).

## Pi RPC Quiescence Drain

Pi spawned sessions complete by quiescence, not by process exit. `SpawnManager` still
owns the generic event loop (persist → observe → fan-out). `PiDrainCoordinator` is a
thin compatibility wrapper around the shared `CompletionCoordinator`. `pi_drain.py`
owns Pi evidence, cleanup, and composition; `pi_completion_profile.py` owns Pi
precedence, phases, deadlines, nudges, and stream-exit policy.

### Ownership Boundary

The Pi completion composition owns:

- parent idle/active observation
- disk watcher / quiescence integration (`PiDiskWatcher`, `PiQuiescenceTracker`)
- active persisted-descendant tracking from the reconciled transitive spawn tree
- pending notification / follow-up tracking
- notification timeout and child-wave timeout decisions
- micro-drain candidate state and phase-event emission coordination
- Pi failure/finalization decisions when the process exits before quiescence

`SpawnManager` should not grow new Pi-specific state-machine branches. Add Pi evidence
or cleanup to the corresponding collaborator in `pi_drain.py`; add precedence, phase,
deadline, nudge, or exit behavior to `pi_completion_profile.py`. The exception is
purely generic event persistence, observer dispatch, subscriber fan-out, heartbeat,
or control-socket handling.

`PiPrivateWorkLedger` owns lifecycle-observed rowless subspawns, process handles,
notifications, managed-bash state, and private-file read failures. It exposes categorized
immutable blocker snapshots and immutable PID/PGID handles. `PiSubspawnTracker` remains
the lifecycle parser/deduplicator and feeds that ledger; `PiDiskWatcher` reads and watches
only bash and notification files. When reconciled descendant evidence finds a row for a
lifecycle-observed subspawn, the ledger keeps the cleanup handle but removes that ID from
rowless liveness. `PiQuiescenceTracker` preserves parent-idle epochs across private-disk
wakeups. The Pi evidence collaborator combines private-work snapshots with reconciled
transitive persisted-descendant evidence; the profile uses the summary for deadlines and
finalization decisions.

Pi and resident use the shared reconciled transitive persisted tree as descendant
authority. A live grandchild beneath a terminal direct child therefore blocks Pi, while a
`finalizing` direct child with a durable report is reconciled terminal and does not.
Only valid, parent-linked rows enter the tree; incomplete and wrong-parent directories
are not descendant evidence. Meridian's `start_spawn()` publishes complete rows
atomically.

### Disk State Authority

Pi extensions coordinate private work with Python through disk files:

- bash state under `runtime_root/pi-bash/<parent>/bash-records.json`
- notification marker under `runtime_root/pi-bash/<parent>/last-notification.json`

Persisted descendant state comes independently from valid rows under
`runtime_root/spawns/` through `ReconciledDescendantEvidence`. Stdout lifecycle-like
messages provide rowless-subspawn/process-handle evidence; they are not persisted-
descendant authority.

Private-disk changes are not passive. `PiDiskWatcher` wakes the drain loop when a bash
or notification file changes, and the drain loop re-evaluates quiescence on those
wakeups. Terminal-event micro-drain re-checks private disk before accepting success;
bounded tree polling rechecks descendant rows while a successful candidate is pending.

An absent private-work file means no blocker. A file that exists but cannot be read or
parsed produces typed unknown evidence instead of an empty snapshot. `done` waits for
that evidence to recover and fails explicitly if it remains unknown through the single
completion deadline.

After a successful terminal candidate, Pi polls the reconciled tree on a bounded cadence
until the candidate completes or the drain ends.

### Child Wave Timeout

When the parent agent is idle and descendant or Pi-private work is still pending,
`PiCompletionProfile` starts the child-wave deadline. If the deadline expires, it fails
with `failed` / `pi_child_wave_timeout` rather than letting Pi wait forever. Timeout
state is latched and its deadline cleared before the single tracked-child cleanup
attempt. Ordinary cleanup or timeout-phase emission failures are diagnostic and do
not replace that outcome or restart waiting-phase emission. If the cleanup await is
cancelled, tracker finalization still runs and cancellation propagates.

### Micro-Drain

When a terminal event arrives but quiescence is not yet confirmed, `PiCompletionProfile`
enters micro-drain mode. It gives already-buffered or just-written disk/event activity a
short chance to arrive before accepting the terminal event as the final outcome. This
covers races where descendant state or notification markers land immediately after
`agent_end`. Micro-drain rechecks both tree and private-disk evidence before finalizing.

### Pi Phase Events

The drain loop emits `meridian.pi.lifecycle.phase` events for Pi-specific milestones.
These are written to `history.jsonl` alongside harness events and are visible in
`meridian spawn show` output:

| Phase | When |
|---|---|
| `drain_started` | Drain loop begins |
| `session_event_seen` / `session_event_absent` | Pi session event observed (or not) |
| `waiting_for_tracked_children` | Parent idle, children still running |
| `waiting_for_notification_completion` | Children done, notifications pending |
| `pi_child_wave_timeout` | Wave deadline expired |
| `pi_notification_timeout` | Notification deadline expired |
| `quiescence_micro_drain_started` | Terminal event seen, polling for quiescence |
| `quiescence_micro_drain_extended` | Additional event during micro-drain |
| `quiescence_deferred` | Terminal event but still waiting for children/notifications |
| `continuation_completed` | Notification resolved on terminal event |
| `cleanup_running` / `cleanup_completed` / `cleanup_escalated` / `cleanup_failed` | Connection cleanup phases |
| `finalized` | Drain complete; final status/exit_code/error |

### Pi Tracked Child Cleanup

When the Pi process exits with active tracked children (crashed, killed, or otherwise
terminated before quiescence), `PiCompletionCleanup` coordinates cleanup through the
tracked child process metadata observed by the evidence collaborator. Process cleanup
lives in `pi_process_cleanup.py` so `SpawnManager` does not own Pi-specific process-tree
policy:

- **POSIX**: iterates captured process group IDs, sends `SIGTERM` via `os.killpg()`,
  waits 250ms, confirms liveness with `os.killpg(pgid, 0)`, then sends `SIGKILL` if
  still alive
- **Windows/fallback**: uses `terminate_tree_sync()` from
  `meridian.lib.platform.process_scope.fallback`

If no process metadata is available, a warning is logged but no cleanup is attempted —
the processes are orphaned. Canonical persisted-descendant cancellation still runs first,
including when the tree contains work that has no Pi lifecycle/process handle.

### Pi Connection Cleanup

Pi connections use the plan-owned `PiDrainSessionTeardown` with a `quiescent` stop reason. The
Pi process receives an abort message (`{"type": "abort"}`) and has a 5-second grace
period to exit. If it doesn't exit within that window, the stop is escalated to
process termination (`SIGTERM` then `SIGKILL`). Cleanup phases are tracked via
`meridian.pi.lifecycle.phase` events for observability.

## Related .context/

- [../../harness/.context/CONTEXT.md](../../harness/.context/CONTEXT.md) — PiAdapter, quiescence completion model, disk-backed coordination state
- [../../harness/connections/.context/CONTEXT.md](../../harness/connections/.context/CONTEXT.md) — PiRpcConnection JSON-RPC transport and event normalization
- [../../ops/spawn/.context/CONTEXT.md](../../ops/spawn/.context/CONTEXT.md) — Pi nested stale detection in query.py
