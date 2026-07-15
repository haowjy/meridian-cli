# lib/streaming/

The async backbone between a live harness connection and the rest of the system.
`SpawnManager` holds the registry of every active spawn; the drain loop is the
core mechanism that moves events from the harness outward to persistence, observers,
and the subscriber queue.

Mostly mechanism. Generic terminal behavior lives in `DrainPolicy`; harness-specific
completion waiting enters through `DrainPlan`. Plain streaming harnesses intentionally
run with `coordinator=None`; Pi and resident Codex/OpenCode paths get narrow
coordinators only when their connection exposes the needed seam. Keep Pi child-wave,
notification, disk-state, resident-done nudges, and tracked-process cleanup policy
behind the coordinator/tracker modules instead of growing `SpawnManager`.

Resident drain selection is capability-driven: a connection participates in the
resident descendant-wait path only when `connection.resident_backend` is present.
Do not key resident behavior off harness ids.

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

`DrainPlan` carries the selected coordinator, policy, aux wake, and finalizer for one active spawn. Its `coordinator=None` value is the plain path. `DrainPolicy` governs terminal event behavior:

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
- `spawn_session.py` — `SpawnSession`, `DrainOutcome`
- `pi_drain.py` — `PiDrainCoordinator`: Pi spawned-session quiescence policy
- `resident_drain.py` — `ResidentDrainCoordinator`: resident-backend descendant waiting policy
- `pi_subspawn_tracker.py` — Pi child-spawn, notification, and wave tracking
- `disk_watcher.py` / `pi_quiescence.py` — disk-backed Pi background-work state
- `drain_wait.py` — generic event/timeout/aux-wake arbitration for drain loops
- `pi_process_cleanup.py` — tracked Pi child process cleanup
- `drain_policy.py` — `DrainPolicy`, `SingleTurnDrainPolicy`, `PersistentDrainPolicy`
- `control_socket.py` — per-spawn inject endpoint
- `event_observers.py` — `EventObserverRegistry`, `EventObserver`, `CallbackObserver`
- `types.py` — `InjectResult`, `ControlMessage`

Resident and Pi do not yet share descendant evidence. Resident reads the
reconciled transitive spawn tree. Pi confirms only direct child rows and also
uses bounded newer-directory uncertainty to cover the current publication
window; that uncertainty is a barrier, not child authority.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — generic drain runtime and lifecycle
→ [.context/pi-drain.md](.context/pi-drain.md) — Pi quiescence and child-wave behavior
→ [.context/signal-cancellation.md](.context/signal-cancellation.md) — cancellation dispatch and scope cleanup

## Related

- `../state/history.py` — `HarnessHistoryWriter` (persistence target for drain loop)
- `../harness/connections/` — `HarnessConnection` protocol (event source)
- `../state/reaper.py` — uses heartbeat sentinel to detect orphaned spawns
