# lib/streaming/

The async backbone between a live harness connection and the rest of the system.
`SpawnManager` holds the registry of every active spawn; the drain loop is the
core mechanism that moves events from the harness outward to persistence, observers,
and the subscriber queue.

Pure mechanism — no spawn policy lives here. Whether to terminate on a given event
is a `DrainPolicy` concern; what spawn to start is a caller concern.

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

## Key Rules

**Never call `connection.send_user_message()` directly.** Always use
`SpawnManager.inject()`. The `ControlActionCoordinator` serializes concurrent
inject, interrupt, permission reply, and user-input-reply actions. Calling the
connection directly breaks this serialization.

**Drain loop ordering is not negotiable: persist → observe → fan-out.** Ten
consecutive write failures abort the loop with a `failed` outcome. If you add a
new stage, it goes after persistence.

**Capture `subprocess_pid` and `scope_snapshot` before `connection.stop()`.** Both
are cleared inside `stop()`. The safety pass that force-kills surviving processes
needs the pre-captured values.

**Do not share a `SpawnManager` instance across concurrent event loops.** The session
dict is not thread-safe. Server paths: one shared manager for the app lifetime.
CLI paths: one short-lived instance per run.

**The terminal `None` sentinel in the subscriber queue is never dropped.** Regular
events are dropped on `QueueFull` (with telemetry). The sentinel evicts one item if
needed to guarantee it gets through — without it, the subscriber hangs forever.

## DrainPolicy

Governs terminal event behavior:

| Policy | On terminal event | Action |
|---|---|---|
| `SingleTurnDrainPolicy` (default) | any | terminate |
| `PersistentDrainPolicy` | `succeeded` | emit `meridian/turn_completed`, continue |
| `PersistentDrainPolicy` | error/cancel | terminate |

Pass `PersistentDrainPolicy` to `start_spawn(drain_policy=...)` for chat sessions
that keep the harness alive across turns.

## Heartbeat / Reaper Contract

`heartbeat_loop` touches `spawns/<id>/heartbeat` every 30 seconds. The reaper
(`lib/state/reaper.py`) identifies orphaned spawns by staleness. A stopped heartbeat
means the manager died or the spawn is orphaned.

## Entry Points

- `spawn_manager.py` — `SpawnManager`: start here for any live-spawn work
- `drain_policy.py` — `DrainPolicy`, `SingleTurnDrainPolicy`, `PersistentDrainPolicy`
- `control_socket.py` — per-spawn inject endpoint
- `event_observers.py` — `EventObserverRegistry`, `EventObserver`, `CallbackObserver`
- `types.py` — `InjectResult`, `ControlMessage`

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — drain loop ordering, `DrainOutcome`
priority, teardown paths, control socket inject flow, heartbeat/reaper contract

## Related

- `../state/history.py` — `HarnessHistoryWriter` (persistence target for drain loop)
- `../harness/connections/` — `HarnessConnection` protocol (event source)
- `../state/reaper.py` — uses heartbeat sentinel to detect orphaned spawns
