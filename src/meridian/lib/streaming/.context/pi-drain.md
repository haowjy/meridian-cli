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
- durable result obligations, causal admission fencing and bounded recovery decisions
- micro-drain candidate state and phase-event emission coordination
- Pi failure/finalization decisions when the process exits before quiescence

`SpawnManager` should not grow new Pi-specific state-machine branches. Add Pi evidence
or cleanup to the corresponding collaborator in `pi_drain.py`; add precedence, phase,
deadline, nudge, or exit behavior to `pi_completion_profile.py`. The exception is
purely generic event persistence, observer dispatch, subscriber fan-out, heartbeat,
or control-socket handling.

`PiPrivateWorkLedger` owns validated managed-bash facts, causal receipts, and
private-file read failures. It exposes categorized immutable blocker snapshots.
`PiDiskWatcher` reads task, receipt, public-observation, explicit-consumption and fault files, while
`PiLifecycleTracker` validates the produced quiescence lifecycle event. Canonical
notification and subspawn events are not part of the Pi runtime contract.
`PiQuiescenceTracker` fences the parent active before acknowledging the exact public
custom-message admission; unrelated activity and wall-clock timestamps cannot acknowledge work. The Pi
evidence collaborator combines private-work snapshots with reconciled transitive
persisted-descendant evidence; the profile uses the summary for deadlines and
finalization decisions.

Pi and resident use the shared reconciled transitive persisted tree as descendant
authority. A live grandchild beneath a terminal direct child therefore blocks Pi, while a
`finalizing` direct child with a durable report is reconciled terminal for liveness.
The same projection retains its raw canonical row: Pi still blocks on result publication
until a terminal row appears, then on result consumption/admission. Resident semantics remain unchanged.
Only valid, parent-linked rows enter the tree; incomplete and wrong-parent directories
are not descendant evidence. Both profiles consume the same immutable cached assessment;
streamed events never trigger a descendant read. Meridian's `start_spawn()` publishes
complete rows atomically.

### Disk State Authority

Pi extensions coordinate private work with Python through disk files:

- bash state under `runtime_root/pi-bash/<parent>/bash-records.json`
- exact admission receipts under `runtime_root/pi-bash/<parent>/delivery-receipts.json`
- Python's exact public-event acknowledgements in `delivery-observations.json`
- explicit CLI observations/dismissals in `observed-spawns.json` / `cleared-spawns.json`
- supervised watcher/storage diagnostics in `delivery-fault.json`

See [the delivery contract](../../../pi_runtime/.context/delivery-contract.md) for full
shapes and writers. `last-notification.json` is retired and ignored. Terminal tracked
background Bash stays owed until its persisted wait marker or exact admission receipt.
Canonical direct-child rows transfer matching launcher obligations, without log parsing.

Persisted descendant state comes independently through `DescendantRefreshOwner`. Its
single-flight worker uses `ReconciledDescendantEvidence` to discover the transitive
subtree from the history index, including archived ancestry, then authoritatively reads
the selected loose rows under `runtime_root/spawns/`. Stdout lifecycle-like subspawn
messages and wake notifications are not descendant evidence.

Private-disk changes are not passive. `PiDiskWatcher` wakes the drain loop when a bash
or delivery file changes, and the drain loop re-evaluates quiescence on those
wakeups. Terminal-event micro-drain rechecks private disk before accepting success;
finish-based bounded refresh rechecks descendants without coupling reads to event volume.

An absent private-work file means no blocker. A file that exists but cannot be read or
validated produces typed unknown evidence instead of an empty snapshot. Boolean,
nonfinite timestamp, wrong-parent, malformed member and version values are rejected.
Unknown evidence and undelivered results have anchored recovery windows (configured
child-wave window, otherwise 300 seconds). Delivery anchors at first idle and is not
evaluated during intentional active-turn deferral; that activity never renews the
anchor. Unknown evidence remains bounded while active too. Failure reports
`pi_evidence_unreadable` with original evidence detail or `pi_delivery_unresolved`.
`done` may release known running execution or descendant liveness once the parent
is idle. It cannot skip a native active turn, an owed result/publication, or unknown
evidence. Result delivery never schedules the generic done nudge; the parent must
receive and finish its causal notice before completion can select its report.

Every proposed success requests a descendant refresh begun after that proposal. Pi
reevaluates policy only after the qualifying result commits; a cached ready result cannot
authorize publication. Refresh completion wakes the existing drain arbitration rather
than acting as lifecycle truth. If the event stream has closed, that arbitration still
enforces refresh, stabilization, nudge, and completion timers.

### Child Wave Timeout

When the parent agent is idle and reconciled descendants are still pending,
`PiCompletionProfile` starts the child-wave deadline. If the deadline expires, it fails
with `failed` / `pi_child_wave_timeout` rather than letting Pi wait forever. Pi-private
bash execution does not start the child-wave deadline. Publication/delivery and unknown
evidence use their separate bounded recovery windows. Child-wave timeout state
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
covers races where descendant state or causal delivery evidence lands immediately after
`agent_end`. Micro-drain rechecks private evidence and requests qualifying descendant
validation before finalizing. A slow initial descendant refresh does not move the
idle/terminal anchor used by Pi's done-nudge delay.

Receipt persistence precedes the native public RPC message event. Until Python observes
that exact delivery ID and membership, evidence remains unknown; once observed, the
parent active fence requires its subsequent idle turn. A crash between receipt and
public observation fails closed with `pi_delivery_event_unobserved`; never fabricate
the missing acknowledgement. Admission plus observation survives cold restart.

### Pi Phase Events

The drain loop emits `meridian.pi.lifecycle.phase` events for Pi-specific milestones.
An inline phase sink atomically updates `spawns/<id>/pi-lifecycle.json`;
`meridian spawn show` reads its latest phase and bounded cleanup status:

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
