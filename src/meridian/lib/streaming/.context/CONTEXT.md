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
| `PiRpcQuiescenceDrainPolicy` | `succeeded` + quiescent | `terminate=True` |
| `PiRpcQuiescenceDrainPolicy` | `succeeded` + not quiescent | emit `meridian/turn_completed`, continue (waiting for children/notifications) |
| `PiRpcQuiescenceDrainPolicy` | error/cancel | `terminate=True` |

`SingleTurnDrainPolicy` is the default. Pass `PersistentDrainPolicy` to `start_spawn(drain_policy=...)` for chat sessions where the harness stays alive across turns. `PiRpcQuiescenceDrainPolicy` is used automatically for Pi spawned sessions (`is_pi_connection and normalized_pi_session_role == "spawned"`) — no explicit policy param needed.

## Pi RPC Quiescence Drain

The Pi spawned drain loop adds quiescence-gated completion on top of standard event
processing. The `_drain_loop()` in `spawn_manager.py` contains Pi-specific logic
behind `pi_quiescence_enabled` / `is_pi_connection` guards.

### 1. Child Subspawn Tracking (`_PiSubspawnTracker`)

`_PiSubspawnTracker` maintains the set of active tracked children by observing
lifecycle events during the drain loop. It handles:

- **Canonical dedup**: lifecycle events carry `(event_type, correlation_id, subspawn_id)`
  dedup keys. Duplicate events (same key seen twice) are silently dropped.
- **Legacy + canonical event support**: accepts both `meridian_subspawn_start` (legacy)
  and `meridian.subspawn.start` (canonical) event types. Anonymous tracked counts
  track legacy events without subspawn IDs.
- **PGID capture**: records process group IDs for cleanup when the wave deadline expires
  or the Pi process exits with active children.
- **Lifecycle invalidation**: if a parse error for a canonical lifecycle event arrives
  with `unsupported_schema_version`, tracking is invalidated and the drain loop fails.

### 2. Notification Timeouts

After children drain, the drain loop may wait for pending notifications to complete.
Each notification has a deadline derived from `MERIDIAN_PI_NOTIFICATION_TIMEOUT_SECONDS`
env var. If a notification's deadline expires before `meridian.notification.completed`
is received, the drain loop fails with `pi_notification_timeout`.

### 3. Child Wave Timeout

When the parent agent becomes idle (`agent_end` → `pi_parent_idle = true`) and tracked
children exist, a child wave deadline is set (`pi_child_wave_deadline_monotonic`). If
children don't finish before this deadline:

- The drain loop calls `_terminate_pi_tracked_subspawns()` — sends `SIGTERM` then
  `SIGKILL` to captured process groups
- The tracker is cleared (`clear_tracked_children_after_wave_timeout()`)
- A followup timeout is set (`pi_child_wave_timeout_followup_deadline`) to wait for
  late-arriving notification events from the Pi process
- If no notification signals arrive before the followup deadline, the drain loop fails
  with `pi_child_wave_timeout`

### 4. Micro-Drain

When a terminal event arrives and quiescence is not yet confirmed, the drain loop
enters micro-drain mode (`pi_quiescence_candidate` is set). Each subsequent event
extends the micro-drain window. The drain loop checks quiescence after every event.
When quiescence IS confirmed, the loop terminates with the candidate outcome. This
handles the race where children/notifications complete between the terminal event
and the quiescence check.

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

### Pi Tracked Subspawn Cleanup

When the Pi process exits with active tracked children (crashed, killed, or otherwise
terminated before quiescence), the drain loop's finally block calls
`_terminate_pi_tracked_subspawns()`:

- **POSIX**: iterates captured process group IDs, sends `SIGTERM` via `os.killpg()`,
  waits 250ms, confirms liveness with `os.killpg(pgid, 0)`, then sends `SIGKILL` if
  still alive
- **Windows/fallback**: uses `terminate_tree_sync()` from
  `meridian.lib.platform.process_scope.fallback`

If no PGID metadata is available (anonymously tracked children), a warning is logged
but no cleanup is attempted — the processes are orphaned.

### Pi Connection Cleanup

Pi connections use `quiescent` stop reason from `_cleanup_completed_session()`. The
Pi process receives an abort message (`{"type": "abort"}`) and has a 5-second grace
period to exit. If it doesn't exit within that window, the stop is escalated to
process termination (`SIGTERM` then `SIGKILL`). Cleanup phases are tracked via
`meridian.pi.lifecycle.phase` events for observability.

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

- [KB: Codebase Guide](../../../../../../../../.meridian/git/meridian-flow-docs/kb/codebase/guide.md) — streaming module orientation and codebase navigation

## Related .context/

- [../../harness/.context/CONTEXT.md](../../harness/.context/CONTEXT.md) — PiAdapter, quiescence completion model, lifecycle events
- [../../harness/connections/.context/CONTEXT.md](../../harness/connections/.context/CONTEXT.md) — PiRpcConnection dual-event-source, PiLifecycleEventTailer
- [../../../pi_runtime/extensions/meridian-lifecycle/.context/CONTEXT.md](../../../pi_runtime/extensions/meridian-lifecycle/.context/CONTEXT.md) — wave/notification system, canonical lifecycle events
- [../../ops/spawn/.context/CONTEXT.md](../../ops/spawn/.context/CONTEXT.md) — Pi nested stale detection in query.py
