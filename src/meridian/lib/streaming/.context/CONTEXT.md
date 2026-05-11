# lib/streaming/ — Context

Async runtime layer between harness connections and the system. Runs from connection
start to terminal state. No policy — pure mechanism.

## Architecture

```
SpawnManager
  ├─ _sessions: dict[SpawnId, SpawnSession]   ← live resources per spawn
  ├─ _history_writers: dict[SpawnId, HarnessHistoryWriter]
  ├─ _observers: EventObserverRegistry
  └─ _heartbeat_tasks: dict[SpawnId, Task]

SpawnSession
  ├─ connection: HarnessConnection   ← live harness connection
  ├─ drain_task: Task                ← background drain loop
  ├─ subscriber: Queue | None        ← single streaming subscriber
  ├─ control_server: ControlSocketServer
  ├─ completion_future: Future[DrainOutcome]
  ├─ cancel_sent: bool               ← double-cancel guard
  └─ control_actions: ControlActionCoordinator
```

## Contracts

### Drain Loop Ordering

Each event flows through three stages in strict order:

1. **Persist** — `HarnessHistoryWriter.write(event)` to `history.jsonl`
2. **Observe** — `EventObserverRegistry.dispatch(spawn_id, event)` (non-blocking)
3. **Fan-out** — `subscriber.put_nowait(event)`

Persistence is synchronous and happens before any notification. 10 consecutive
write failures abort the loop with a `failed` outcome. Do not reorder these stages
— observers and the subscriber must only see events that are durably written.

### DrainOutcome Priority

When the drain loop exits, the outcome is determined in this order:

1. `asyncio.CancelledError` raised → `cancelled`
2. Unhandled exception → `failed`
3. `session.cancel_sent` is True → `cancelled` (exit code 143)
4. `recorded_terminal_outcome` from harness → use its status/exit_code/error
5. Connection closed without a terminal event → `failed` with `connection_closed_without_terminal_event`

### Subscriber Queue Backpressure

The subscriber queue (`maxsize=1000`) drops non-sentinel events on `QueueFull`
with telemetry. The terminal `None` sentinel is **never dropped** — on full queue,
the implementation evicts one item to force the sentinel through.

### SpawnManager Singleton Pattern

One `SpawnManager` lives for the app server lifetime. Spawn CLI paths create a
short-lived instance per run. Do not share manager instances between runs — the
session dict is not concurrency-safe across multiple concurrent `start_spawn` calls
from different event loops.

## Lifecycle

```
start_spawn()
  → dispatch_start()           — creates HarnessConnection, calls connection.start()
  → _drain_loop()              — asyncio.Task, until terminal or error
  → ControlSocketServer.start() — binds per-spawn control endpoint
  → _start_heartbeat()         — asyncio.Task, touches sentinel every 30s
```

Teardown paths:
- **Natural completion**: drain loop exits on terminal event → `_cleanup_completed_session()`
- **Explicit stop**: `stop_spawn()` → interrupt → emit synthetic cancelled event → stop connection → join drain task (2s timeout, then cancel)
- **Shutdown**: `shutdown()` calls `stop_spawn()` on all sessions

Capture `subprocess_pid` and `scope_snapshot` **before** calling `connection.stop()` —
both are cleared inside `stop()`. A safety pass after `stop()` uses the pre-captured
values to force-kill any surviving process tree.

## Control Socket

Inject flows through the control socket:

```
meridian spawn inject <id> --message "..."
  → control.sock (POSIX) or control.sock.port (Windows)
  → ControlSocketServer._handle_client()
  → SpawnManager.inject()
    → ControlActionCoordinator.run_action()  — serializes concurrent actions
    → _record_inbound()                       — appends to inbound.jsonl
    → connection.send_user_message(message)
```

All control actions (inject, interrupt, permission reply, user input reply) are
serialized through `ControlActionCoordinator`. Concurrent actions queue behind it —
do not call `connection.send_user_message()` directly.

## Heartbeat/Reaper Contract

`heartbeat_loop` touches `spawns/<id>/heartbeat` every 30 seconds. The reaper
(`lib/state/reaper.py`) identifies orphaned spawns by checking heartbeat staleness
during doctor runs. A heartbeat that stops updating means the spawn is orphaned or
the manager died.

The `touch` callable is injectable for tests. The `heartbeat_interval_secs` parameter
is configurable on `SpawnManager`.

## DrainPolicy

Two policies control terminal event behavior:

| Policy | Terminal event | Action |
|---|---|---|
| `SingleTurnDrainPolicy` | any | `terminate=True` |
| `PersistentDrainPolicy` | `succeeded` | emit `meridian/turn_completed`, continue |
| `PersistentDrainPolicy` | error/cancel | `terminate=True` |

`SingleTurnDrainPolicy` is the default. Pass `PersistentDrainPolicy` to `start_spawn(drain_policy=...)` for chat sessions where the harness stays alive across turns.

## SignalCanceller

`SignalCanceller` (`signal_canceller.py`) is the two-lane cancel dispatcher: CLI spawns
and app-managed spawns take different paths, but both converge on a terminal state read.

### Cancel Dispatch

`cancel()` routes by `launch_mode`:

- **CLI spawns** → `_cancel_cli_spawn()` — reads scope sidecars, terminates process groups
- **App spawns** → `_cancel_app_spawn()` — delegates to `SpawnManager.stop_spawn()` if a
  manager is present; falls back to an HTTP cancel against the running app socket otherwise

### `_cancel_cli_spawn()` — Scope-Aware Path

The method reads scope sidecars first rather than resolving a PID directly:

1. **Read** scope sidecars via `read_scopes_from_disk()` — written at spawn time, describe
   the process groups/trees the spawn owns
2. **If scopes exist**: iterate them, skip already-released scopes via `is_scope_released()`,
   call `terminate_scope_sync()` per scope (POSIX uses pgid group kill; Windows falls back to
   tree kill), then call `mark_scope_released()` immediately after to prevent double-kill
3. **If no scopes (legacy)**: resolve runner PID from `record.runner_pid` /
   `record.worker_pid`, fall back to `terminate_tree_sync()` directly — preserves
   compatibility with spawns that predate scope sidecar support

`terminate_scope_sync` is synchronous, so each call runs via `asyncio.to_thread()` to
avoid blocking the event loop. `ProcessLookupError` is suppressed — the process may
already be gone by the time the cancel arrives.

After signal delivery, `_wait_for_terminal()` polls the spawn record for up to
`grace_seconds`. If the record never reaches a terminal status, the outcome carries
`finalizing=True` — the caller must not treat this as a confirmed stop.

### Dependency Direction

`signal_canceller` depends on `platform/` and `state/` — it does not depend on
`core.process_cleanup`. The cancel path has a live event loop and manages scope cleanup
inline. `core.process_cleanup` is the sync-only reclamation path used at startup for
orphan recovery — the two paths don't share scope management logic.

## Anti-Patterns

- **Don't call `connection.send_user_message()` directly** — always go through `SpawnManager.inject()` so the action coordinator serializes it.
- **Don't observe events before they're persisted** — the drain loop ordering is the persistence guarantee. Breaking it means observers may see events that weren't written to disk.
- **Don't share a manager across concurrent event loops** — the session dict is not thread-safe.
- **Don't add scope cleanup after `SignalCanceller.cancel()`** — the canceller handles scope termination internally via the scope-sidecar path. Duplicate cleanup causes double-kill races.

## Related KB

- [KB: Streaming Subsystem](../../../../../../../../.meridian/git/meridian-flow-docs/kb/codebase/streaming-subsystem.md) — cross-cutting streaming design
