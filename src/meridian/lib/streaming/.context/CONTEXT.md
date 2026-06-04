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

The implementation is split by responsibility:

- `spawn_manager.py` — public registry/control API and generic live-spawn lifecycle
- `spawn_dispatch.py` — connection creation/start dispatch
- `spawn_drain_loop.py` — drain loop, persistence/observer/fan-out ordering, outcome priority
- `spawn_session.py` — `SpawnSession` and `DrainOutcome` carriers
- `pi_drain.py` — Pi spawned-session quiescence coordinator
- `pi_subspawn_tracker.py` — Pi child-spawn and notification tracking
- `disk_watcher.py` / `pi_quiescence.py` — disk-backed Pi quiescence inputs
- `drain_wait.py` — bounded wait helpers for drain/cleanup paths
- `pi_process_cleanup.py` — tracked Pi child process cleanup

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
  → SpawnDrainLoop.run()       — asyncio.Task, until terminal or error
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

Generic policies control terminal event behavior:

| Policy | Terminal event | Action |
|---|---|---|
| `SingleTurnDrainPolicy` | any | `terminate=True` |
| `PersistentDrainPolicy` | `succeeded` | emit `meridian/turn_completed`, continue |
| `PersistentDrainPolicy` | error/cancel | `terminate=True` |

`SingleTurnDrainPolicy` is the default. Pass `PersistentDrainPolicy` to
`start_spawn(drain_policy=...)` for chat sessions where the harness stays alive
across turns. Pi spawned-session behavior is intentionally not another generic
policy flag: it is delegated to `PiDrainCoordinator`, because it needs disk-backed
background-work state, child-wave deadlines, notification state, and cleanup decisions.

## Pi RPC Quiescence Drain

Pi spawned sessions complete by quiescence, not by process exit. `SpawnManager` still
owns the generic event loop (persist → observe → fan-out), but Pi-specific decisions
live in `pi_drain.py:PiDrainCoordinator`.

### Ownership Boundary

`PiDrainCoordinator` owns:

- parent idle/active observation
- disk watcher / quiescence integration (`PiDiskWatcher`, `PiQuiescenceTracker`)
- active child spawn tracking from disk-backed child rows
- pending notification / follow-up tracking
- notification timeout and child-wave timeout decisions
- micro-drain candidate state and phase-event emission coordination
- Pi failure/finalization decisions when the process exits before quiescence

`SpawnManager` should not grow new Pi-specific state-machine branches. Add Pi drain
behavior to `PiDrainCoordinator` unless the change is purely generic event persistence,
observer dispatch, subscriber fan-out, heartbeat, or control-socket handling.

`PiSubspawnTracker` owns the mutable child-spawn view inside the coordinator. It filters
unresolved stale candidates to numeric allocated-looking `p*` directories, tracks
active child ids, preserves idle epochs across disk wakeups, and marks whether a child
wave needs re-arming. `PiDrainCoordinator` uses that summarized state to make deadline
and finalization decisions.

### Disk State Authority

Pi extensions coordinate with Python through disk files:

- child spawn records under `runtime_root/spawns/<child>/state.json`
- bash state under `runtime_root/pi-bash/<parent>/bash-records.json`
- notification marker under `runtime_root/pi-bash/<parent>/last-notification.json`

Stdout lifecycle-like messages are diagnostic. They are not the state authority for
quiescence.

Disk changes are not passive. `PiDiskWatcher` wakes the drain loop when a watched file
changes, and the drain loop re-evaluates quiescence on those wakeups. Terminal-event
micro-drain re-checks disk before accepting success so a just-written child row,
bash update, or notification marker cannot be missed.

### Child Wave Timeout

When the parent agent is idle and disk-backed child/background work is still pending,
`PiDrainCoordinator` starts the child-wave deadline. If the deadline expires, it fails
or finalizes according to the tracked disk state and cleanup outcome rather than letting
Pi wait forever.

### Micro-Drain

When a terminal event arrives but quiescence is not yet confirmed, `PiDrainCoordinator`
enters micro-drain mode. It gives already-buffered or just-written disk/event activity a
short chance to arrive before accepting the terminal event as the final outcome. This
covers races where child state or notification markers land immediately after
`agent_end`. Micro-drain is disk-aware: timeout alone is not enough to finalize until
the latest disk-backed state has been read.

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
terminated before quiescence), `PiDrainCoordinator` coordinates cleanup through the
tracked child process metadata it has observed. Process cleanup lives in
`pi_process_cleanup.py` so `SpawnManager` does not own Pi-specific process-tree policy:

- **POSIX**: iterates captured process group IDs, sends `SIGTERM` via `os.killpg()`,
  waits 250ms, confirms liveness with `os.killpg(pgid, 0)`, then sends `SIGKILL` if
  still alive
- **Windows/fallback**: uses `terminate_tree_sync()` from
  `meridian.lib.platform.process_scope.fallback`

If no process metadata is available, a warning is logged but no cleanup is attempted —
the processes are orphaned.

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

- **CLI spawns** → `_cancel_cli_spawn()` — resolves runner PID first, signals it;
  falls back to scope cleanup only when the runner is absent or dead.
- **App spawns** → `_cancel_app_spawn()` — delegates to `SpawnManager.stop_spawn()` if a
  manager is present; falls back to an HTTP cancel against the running app socket otherwise.

`SignalCanceller` does **not** claim terminal lifecycle authority. It delivers cancel
signals and returns delivery facts (`finalizing: bool`, `already_terminal: bool`).
The `SpawnApplicationService.cancel()` path owns convergence to terminal — it calls
`_force_cancel_convergence()` when delivery alone is insufficient.

### `_cancel_cli_spawn()` — Runner-First, Scope-Fallback Path

The method resolves `record.runner_pid` first and signals that runner tree. If the
runner is absent/dead, or if runner termination does not produce terminal state, it
falls back to `process_cleanup.terminate_spawn_scopes()` via `asyncio.to_thread()`
when no live runner is available. After a guarded runner-tree signal, it calls
`process_cleanup.terminate_recorded_spawn_scopes()` so real scope records still clean
up without re-running the legacy worker fallback. The cleanup path owns scope policy:

1. Reads scope sidecars via `read_scopes_from_disk()`.
2. Skips already-released scopes by concrete `release_id`.
3. Preserves live `session_owned` scopes via `should_skip_cleanup()`.
4. Terminates remaining scopes through `terminate_scope_sync()` and marks each
   concrete `release_id` released.
5. Falls back to legacy `worker_pid` termination only through `terminate_spawn_scopes()`
   when no sidecars exist.

After signal delivery, `_wait_for_terminal()` polls the spawn record for up to
`grace_seconds`. If the record never reaches a terminal status, the outcome carries
`finalizing=True` — the caller must not treat this as a confirmed stop.

### Dependency Direction

`signal_canceller` depends on `platform/` and `state/` — it does not depend on
`core.spawn_service` or own lifecycle finalization. The cancel path has a live event
loop and manages scope cleanup inline. `core.process_cleanup` is the sync-only
reclamation path used at startup for orphan recovery — the two paths don't share
scope management logic.

## Anti-Patterns

- **Don't call `connection.send_user_message()` directly** — always go through `SpawnManager.inject()` so the action coordinator serializes it.
- **Don't observe events before they're persisted** — the drain loop ordering is the persistence guarantee. Breaking it means observers may see events that weren't written to disk.
- **Don't share a manager across concurrent event loops** — the session dict is not thread-safe.
- **Don't add scope cleanup after `SignalCanceller.cancel()`** — the canceller handles scope termination internally via the scope-sidecar path. Duplicate cleanup causes double-kill races.

## Related KB

KB lives at `$MERIDIAN_CONTEXT_KB_DIR` (see `meridian context kb`). Use the
codebase guide there for broader streaming module orientation.

## Related .context/

- [../../harness/.context/CONTEXT.md](../../harness/.context/CONTEXT.md) — PiAdapter, quiescence completion model, disk-backed coordination state
- [../../harness/connections/.context/CONTEXT.md](../../harness/connections/.context/CONTEXT.md) — PiRpcConnection JSON-RPC transport and event normalization
- [../../ops/spawn/.context/CONTEXT.md](../../ops/spawn/.context/CONTEXT.md) — Pi nested stale detection in query.py
