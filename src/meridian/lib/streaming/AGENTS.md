# lib/streaming/

The async backbone between a live harness connection and the rest of the system.
`SpawnManager` holds the registry of every active spawn; the drain loop is the
core mechanism that moves events from the harness outward to persistence, observers,
and the subscriber queue.

Mostly mechanism. Generic terminal classification lives in `DrainPolicy`; shared
candidate/deadline/stabilization mechanics live in `CompletionCoordinator`, behind
the outer `DrainCoordinator` seam. Plain streaming harnesses intentionally run with
`coordinator=None`; Pi and resident Codex/OpenCode paths get narrow coordinators only
when their connection exposes the needed seam. Keep Pi child-wave,
notification, disk-state, resident-done nudges, and tracked-process cleanup policy
behind the coordinator/tracker modules instead of growing `SpawnManager`.

`drain_plan_factory.py` is the composition root for plan selection. It owns the
plain/resident/Pi choice and injects manager capabilities for event emission,
serialized injection, and application-service descendant cleanup into both Pi and
resident policy. Drain collaborators must not construct application services.

Resident drain selection is capability-driven: a connection participates in the
resident descendant-wait path only when `connection.resident_backend` is present.
Do not key resident behavior off harness ids.

## Completion and Absolute Timeout Models

Resident Codex/OpenCode completion uses an absolute deadline per arm. Only an explicit
`rearm.signal` can grant a new window; ordinary harness or descendant activity cannot.
Rearm grants are unlimited by default. `--resident-rearm-budget`,
`MERIDIAN_RESIDENT_REARM_BUDGET`, profile `resident-rearm-budget`, or
`timeouts.resident_rearm_budget` opts into a maximum number of granted extensions.
The running grant count is persisted as `state.json`'s `resident_rearm_count`.

Pi completion is descendant-quiescence-driven and intentionally has no default total
wall-clock bound. Notification and child-wave windows are anchored to each notification
or child wave, not slid by ordinary activity; successive legitimate waves may establish
new windows. This unbounded-while-descendants-live behavior is by design.

For either profile, `--timeout` / `MERIDIAN_TIMEOUT` is the shared opt-in absolute
ceiling. It arms the non-renewing outer attempt timer in `streaming_runner.py` and
defaults to `None`; resident rearms and Pi waves cannot reset it.

## Mental Model

```
HarnessConnection  →  drain loop  →  1. persist (HarnessHistoryWriter)
                                     2. observe (EventObserverRegistry)
                                     3. fan-out (subscriber queue)
```

The ordering is a contract, not an implementation detail. Observers and the subscriber
must only see events that are durably written. Breaking the order means a crash between
steps 1 and 2 could leave observers with data the persistence layer never recorded.

`SpawnManager` is the integration point for everything that touches a live spawn:
starting, stopping, injecting messages, subscribing to events, and tracking heartbeats.
Do not bypass it to reach a connection or subscriber directly.

`connection.primary_event_scope` is part of terminal classification for multiplexed
streams. The drain loop still persists and broadcasts child Codex/OpenCode events,
but semantic helpers must receive the scope so child terminal frames cannot finish,
fail, or clear signals for the parent.

## Key Rules

**Never call `connection.send_user_message()` directly.** Always use
`SpawnManager.inject()`. The `ControlActionCoordinator` serializes concurrent
inject, interrupt, permission reply, and user-input-reply actions. Calling the
connection directly breaks this serialization.

**Drain loop ordering is not negotiable: persist → observe → fan-out.** A failed
write — including the tenth consecutive failure that aborts the loop with a
`failed` outcome — is never delivered to the coordinator, observers, or the
subscriber. If you add a new stage, it goes after successful persistence.

**Capture `subprocess_pid` and `scope_snapshot` before `connection.stop()`.** Both
are cleared inside `stop()`. The safety pass that force-kills surviving processes
needs the pre-captured values.

**Do not share a `SpawnManager` instance across concurrent event loops.** The session
dict is not thread-safe. Server paths: one shared manager for the app lifetime.
CLI paths: one short-lived instance per run.

**The terminal `None` sentinel in the subscriber queue is never dropped.** Regular
events are dropped on `QueueFull` (with telemetry). The sentinel evicts one item if
needed to guarantee it gets through — without it, the subscriber hangs forever.

## DrainPlan / DrainPolicy

`DrainPlan` carries the selected coordinator, policy, aux wake, finalizer, and async
session teardown for one active spawn. Its `coordinator=None` value is the plain path.
`DrainPolicy` governs terminal event behavior:

| Policy | On terminal event | Action |
|---|---|---|
| `SingleTurnDrainPolicy` (default) | any | terminate |
| `PersistentDrainPolicy` | `succeeded` | emit `meridian/turn_completed`, continue |
| `PersistentDrainPolicy` | error/cancel | terminate |

Pass `PersistentDrainPolicy` to `start_spawn(drain_policy=...)` for long-lived
session drains that keep the harness alive across turns.

## Heartbeat / Reaper Contract

`heartbeat_loop` touches `spawns/<id>/heartbeat` every 30 seconds. The reaper
(`lib/state/reaper.py`) identifies orphaned spawns by staleness. A stopped heartbeat
means the manager died or the spawn is orphaned.

## Entry Points

- `spawn_manager.py` — `SpawnManager`: public live-spawn registry/control API
- `spawn_dispatch.py` — connection creation/start dispatch
- `spawn_drain_loop.py` — event drain, persistence/observer/fan-out ordering, outcome priority
- `drain_coordinator.py` — `DrainCoordinator` protocol seam for harness-specific completion policy
- `drain_plan_factory.py` — plain/resident/Pi plan selection and capability wiring
- `drain_teardown.py` — harness-neutral plan-owned connection-stop contract and default
- `pi_drain_teardown.py` — Pi connection-stop cleanup-phase policy
- `completion_contracts.py` — typed evidence, profile, and cleanup collaborator contracts
- `completion_coordinator.py` — shared candidate/wait/deadline/stabilization state machine
- `descendant_evidence.py` — shared reconciled, transitive persisted-descendant assessment
- `spawn_session.py` — `SpawnSession`, `DrainOutcome`
- `pi_completion_profile.py` — Pi precedence, phases, deadlines, nudges, and stream-exit
  policy
- `pi_drain.py` — Pi evidence/cleanup collaborators and drain-protocol adapter
- `pi_work_ledger.py` — sole mutable owner of Pi-private blockers and PID/PGID cleanup
  handles; exposes immutable categorized snapshots
- `resident_drain.py` — resident evidence/profile/cleanup and drain-protocol adapter
- `pi_subspawn_tracker.py` — Pi lifecycle parsing/deduplication; feeds the private-work ledger
- `disk_watcher.py` / `pi_quiescence.py` — Pi-private bash/notification disk
  observation and parent-idle epochs; disk-backed private evidence feeds the ledger
- `drain_wait.py` — generic event/timeout/aux-wake arbitration for drain loops
- `pi_process_cleanup.py` — tracked Pi child process cleanup
- `drain_policy.py` — `DrainPolicy`, `SingleTurnDrainPolicy`, `PersistentDrainPolicy`
- `control_socket.py` — per-spawn inject endpoint
- `event_observers.py` — `EventObserverRegistry`, `EventObserver`, `CallbackObserver`
- `types.py` — `InjectResult`, `ControlMessage`

Resident and Pi completion use the shared reconciled transitive spawn-tree assessment as
their sole persisted-descendant authority. Pi's disk watcher observes only private bash
and notification files; incomplete or wrong-parent spawn directories are not descendant
evidence. Meridian's own spawn rows publish atomically, and the reconciled tree polls
valid parent-linked rows while a successful terminal candidate is pending.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — generic drain runtime and lifecycle
→ [.context/pi-drain.md](.context/pi-drain.md) — Pi quiescence and child-wave behavior
→ [.context/signal-cancellation.md](.context/signal-cancellation.md) — cancellation dispatch and scope cleanup

## Related

- `../state/history.py` — `HarnessHistoryWriter` (persistence target for drain loop)
- `../harness/connections/` — `HarnessConnection` protocol (event source)
- `../state/reaper.py` — uses heartbeat sentinel to detect orphaned spawns
