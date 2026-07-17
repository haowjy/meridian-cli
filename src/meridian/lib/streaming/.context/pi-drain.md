# lib/streaming/ — Pi Drain Context

Pi-specific quiescence and tracked-child behavior for spawned Pi sessions. The
generic streaming runtime remains in [CONTEXT.md](CONTEXT.md).

## Pi RPC Quiescence Drain

Pi spawned sessions complete by quiescence, not by process exit. `SpawnManager` still
owns the generic event loop (persist → observe → fan-out). `PiDrainCoordinator` adapts
the Pi collaborators to the shared `CompletionCoordinator`. `pi_drain.py`
owns Pi evidence and cleanup collaborators; `pi_completion_profile.py` owns Pi
precedence, phases, deadlines, nudges, and stream-exit policy.
`drain_plan_factory.py` is the composition root for the full Pi drain plan.

### Ownership Boundary

The Pi completion composition owns:

- parent idle/active observation
- disk watcher / quiescence integration (`PiDiskWatcher`, `PiQuiescenceTracker`)
- active persisted-descendant tracking from the reconciled transitive spawn tree
- direct follow-up marker gating and child-wave timeout decisions
- micro-drain candidate state and phase-event emission coordination
- Pi failure/finalization decisions when the process exits before quiescence

`SpawnManager` should not grow new Pi-specific state-machine branches. Add Pi evidence
or cleanup to the corresponding collaborator in `pi_drain.py`; add precedence, phase,
deadline, nudge, or exit behavior to `pi_completion_profile.py`. The exception is
purely generic event persistence, observer dispatch, subscriber fan-out, heartbeat,
or control-socket handling.

`PiPrivateWorkLedger` owns managed-bash state, the direct follow-up marker, and
private-file read failures. It exposes categorized immutable blocker snapshots.
`PiDiskWatcher` reads and watches the bash and notification-marker files, while
`PiLifecycleTracker` validates the produced quiescence lifecycle event. Canonical
notification and subspawn events are not part of the Pi runtime contract.
`PiQuiescenceTracker` preserves parent-idle epochs across private-disk wakeups. The Pi
evidence collaborator combines private-work snapshots with reconciled transitive
persisted-descendant evidence; the profile uses the summary for deadlines and
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
subspawn messages are not descendant evidence.

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

When the parent agent is idle and reconciled descendants are still pending,
`PiCompletionProfile` starts the child-wave deadline. If the deadline expires, it fails
with `failed` / `pi_child_wave_timeout` rather than letting Pi wait forever. Pi-private
bash and notification-marker work does not start this deadline; it relies on direct
follow-up/nudge handling and the opt-in outer attempt timeout. Child-wave timeout state
is latched and its deadline cleared before the outcome publishes. The single
descendant cleanup then runs asynchronously and best-effort. Ordinary cleanup or
timeout-phase emission failures are diagnostic and do not replace that outcome or
restart waiting-phase emission. Startup reaper reconciliation recovers cleanup
interrupted by a crash.

Child-wave windows are anchored when their corresponding wave begins; ordinary
descendant disk evidence does not slide them. Pi has no default total wall-clock
ceiling: descendant quiescence may require successive waves, each with its own anchored
window, and that unbounded total duration is intentional.
Operators who need an absolute bound use the shared `--timeout` /
`MERIDIAN_TIMEOUT` outer attempt timer, which is non-renewing and defaults to `None`.
Resident completion uses the same outer ceiling but otherwise follows its separate
signal-gated deadline/rearm model documented in [AGENTS.md](../AGENTS.md).

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
| `pi_child_wave_timeout` | Wave deadline expired |
| `quiescence_micro_drain_started` | Terminal event seen, polling for quiescence |
| `quiescence_micro_drain_extended` | Additional event during micro-drain |
| `quiescence_deferred` | Terminal event but still waiting for children/private disk evidence |
| `cleanup_running` / `cleanup_completed` / `cleanup_escalated` / `cleanup_failed` | Connection cleanup phases |
| `finalized` | Drain complete; final status/exit_code/error |

### Pi Tracked Child Cleanup

When the Pi process exits with active tracked descendants (crashed, killed, or otherwise
terminated before quiescence), `PiCompletionCleanup` invokes the injected descendant
cancellation service. Persisted spawn rows are the sole child authority; cleanup does
not depend on unproduced lifecycle PID/PGID telemetry.

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
