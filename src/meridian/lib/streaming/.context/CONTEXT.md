# lib/streaming/ — Context

Async runtime layer between harness connections and the system. It owns generic
transport and live-spawn lifecycle mechanism from connection start to terminal
state, while `DrainPlan` delegates terminal and harness-specific completion policy.
Harness-specific decisions stay behind `DrainPolicy`, `DrainCoordinator`, and their
Pi/resident modules rather than entering the generic loop or `SpawnManager`.

## Architecture

```
SpawnManager
  ├─ _sessions: dict[SpawnId, SpawnSession]   ← live resources per spawn
  ├─ _history_writers: dict[SpawnId, HarnessHistoryWriter]
  ├─ _observers: EventObserverRegistry
  └─ _heartbeat_tasks: dict[SpawnId, Task]

DrainPlan
  ├─ coordinator: DrainCoordinator | None
  ├─ policy: DrainPolicy
  ├─ aux_wake / handle_aux_wake
  ├─ finalizer
  └─ teardown: DrainSessionTeardown

SpawnSession
  ├─ connection: HarnessConnection   ← live harness connection
  ├─ drain_task: Task                ← background drain loop
  ├─ subscriber: Queue | None        ← single streaming subscriber
  ├─ control_server: ControlSocketServer
  ├─ completion_future: Future[DrainOutcome]
  ├─ teardown: DrainSessionTeardown
  ├─ drain_plan: DrainPlan           ← full plan for terminal publication
  ├─ cancel_sent: bool               ← double-cancel guard
  ├─ control_actions: ControlActionCoordinator
  ├─ authoritative_stop_outcome: DrainOutcome | None  ← explicit stop intent
  ├─ terminal_published: bool        ← idempotent publication guard
  └─ cleanup_task: Task | None       ← per-spawn post-publication teardown
```

The implementation is split by responsibility:

- `spawn_manager.py` — public registry/control API and generic live-spawn lifecycle
- `drain_plan_factory.py` — plain/resident/Pi plan selection and capability wiring
- `spawn_dispatch.py` — connection creation/start dispatch
- `spawn_drain_loop.py` — drain loop, persistence/observer/fan-out ordering, outcome priority
- `drain_coordinator.py` — `DrainPlan` plus the narrow `DrainCoordinator` seam
- `drain_teardown.py` — harness-neutral plan-owned async connection-stop contract
- `pi_drain_teardown.py` — Pi cleanup-phase connection-stop policy
- `spawn_session.py` — `SpawnSession` and `DrainOutcome` carriers
- `resident_drain.py` — resident-backend descendant waiting and done-nudge model
- `pi_drain.py` — Pi spawned-session quiescence coordinator
- `pi_lifecycle_tracker.py` — validation for produced Pi lifecycle events
- `disk_watcher.py` / `pi_quiescence.py` — disk-backed Pi quiescence inputs
- `drain_wait.py` — bounded wait helpers for drain/cleanup paths

## Contracts

### Drain Loop Ordering

Each event flows through three stages in strict order:

1. **Persist** — `HarnessHistoryWriter.write(event)` to `history.jsonl`
2. **Observe** — `EventObserverRegistry.dispatch(spawn_id, event)` (non-blocking)
3. **Fan-out** — `subscriber.put_nowait(event)`

Persistence is synchronous and happens before any notification. 10 consecutive
write failures abort the loop with a `failed` outcome. Do not reorder these stages
— observers and the subscriber must only see events that are durably written.
Coordinator post-persistence handling (`note_event_persisted`) is gated the same
way: a failed write, including the tenth failure that aborts the loop, is not
delivered to the coordinator, observers, or subscriber.

Terminal classification happens after persistence. The drain loop passes
`connection.primary_event_scope` into the harness semantic helpers when a connection
provides one. This matters for multiplexed streams: child Codex threads and child
OpenCode task sessions are still written to `history.jsonl` and sent to observers,
but their terminal events do not complete/fail the parent, clear parent signals, or
drive parent activity transitions.

### DrainPlan Selection

`drain_plan_factory.build_drain_plan()` returns the whole drain-loop configuration
for one active spawn. `SpawnManager._select_drain_plan()` only supplies manager-owned
capabilities and delegates to it. Codex/OpenCode resident backends get a
`ResidentDrainCoordinator`; Pi RPC gets `PiDrainCoordinator` plus disk-watcher aux
wakes; plain streaming harnesses intentionally run with
`DrainPlan(coordinator=None)`. There is no public no-op coordinator class — absence
of a coordinator is the plain path.

The factory also owns application-service construction for descendant cancellation
and injects that capability into both resident and Pi cleanup. Harness-policy modules
must remain consumers of the capability rather than importing bootstrap builders.

`DrainCoordinator` stays narrow: it observes events, handles terminal events,
provides timeouts, classifies stream close, and handles stream exit. Constant
configuration belongs in `DrainPlan`, not in coordinator methods.

Scope filtering happens before coordinator terminal handling. A child OpenCode
`session.idle` / `session.error` produces no `TerminalEventOutcome` for the parent,
so resident and plain drain paths never see it as a parent completion candidate.
`PrimaryEventScope` is the only scope contract passed through the drain stack; do
not preserve or add compatibility side channels such as a Codex-only thread-id path.

### DrainOutcome Classification

The drain loop classifies its outcome, but does not own publication.
Classification order:

1. `recorded_terminal_outcome` with `status == "succeeded"` → `succeeded`
   (success takes priority over cancellation)
2. `asyncio.CancelledError` raised → `cancelled`
3. Unhandled exception → `failed`
4. `session.cancel_sent` is True → `cancelled` (exit code 143)
5. Non-success `recorded_terminal_outcome` from harness → use its status/exit_code/error
6. Connection closed without a terminal event → `failed` with `connection_closed_without_terminal_event`

After classification, `SpawnManager._publish_terminal()` applies
`resolve_terminal_outcome()` which resolves competing sources: success wins;
otherwise an authoritative stop outcome (from `stop_spawn()`) wins; otherwise the
drain classification stands. Publication is idempotent — `terminal_published` on
`SpawnSession` guards against double publication.

Resident completion also resolves transport death before that publication barrier.
When a successful turn is being held for persisted descendant work and the backend
then emits a generic `error/connectionClosed`, the resident profile publishes
`backend_dead_while_awaiting_done` rather than the transport's incidental close
message. Harness-specific terminal failures remain authoritative. A success already
recorded for publication is still protected by the generic success-first barrier.

### Subscriber Queue Backpressure

The subscriber queue (`maxsize=1000`) drops non-sentinel events on `QueueFull`
with telemetry. The terminal `None` sentinel is **never dropped** — on full queue,
the implementation evicts one item to force the sentinel through.

### SpawnManager Lifetime

Each streaming run creates a short-lived `SpawnManager`. Do not share manager
instances between runs — the session dict is not concurrency-safe across multiple
concurrent `start_spawn` calls from different event loops.

### Terminal Publication and Recovery

`SpawnManager._publish_terminal()` is the idempotent barrier that owns terminal
lifecycle publication. It resolves competing terminal sources via
`resolve_terminal_outcome()` (success > authoritative stop > drain classification),
runs the plan finalizer, resolves the completion future, notifies observers, and
starts one per-spawn cleanup task. Both the drain loop's natural exit path and
`stop_spawn()` call it; the `terminal_published` guard ensures exactly one publication.

Cleanup tasks are keyed per spawn (`_cleanup_tasks: dict[SpawnId, Task]`).
`stop_spawn()` awaits only its own spawn's cleanup. `shutdown()` drains all
remaining cleanup tasks. This prevents cross-spawn coupling where stopping one
spawn would block on another's unrelated cleanup.

Terminal outcomes publish before plan-owned teardown runs asynchronously and best-effort;
startup reaper reconciliation recovers incomplete cleanup. Completion deadlines use
process-local monotonic time and are not persisted or crash-stable.

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
  → per-user temp directory / hashed control socket
  → ControlSocketServer._handle_client()
  → SpawnManager.inject()
    → ControlActionCoordinator.run_action()  — serializes concurrent actions
    → _record_inbound()                       — appends to inbound.jsonl
    → connection.send_user_message(message)
```

On POSIX, both the server and inject client derive the same bounded socket path from
the resolved runtime root and spawn ID. The socket lives in a mode-0700, UID-scoped
directory under the system temp root, so runtime/project path depth cannot exceed
`sockaddr_un.sun_path` and concurrent spawns remain isolated. The legacy
native-Windows branch uses `control.sock.port` and is untested.

All control actions (inject, interrupt, permission reply, user input reply) are
serialized through `ControlActionCoordinator`. Concurrent actions queue behind it —
do not call `connection.send_user_message()` directly.

## Heartbeat/Reaper Contract

`heartbeat_loop` touches `spawns/<id>/heartbeat` every 30 seconds. The reaper
(`lib/state/reaper.py`) identifies orphaned spawns by checking heartbeat staleness
during doctor runs. The loop stops without recreating its parent when the published
spawn directory is deleted. Unexpected cessation while a spawn should remain active
means the spawn is orphaned or the manager died; normal completion, cancellation,
and manager shutdown also stop the task intentionally.

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
across turns. Pi spawned-session behavior and resident descendant waiting are not
generic policy flags: `SpawnManager` selects coordinators through `DrainPlan` when
the connection exposes the needed seam.

## Specialized Context

- [Pi drain](pi-drain.md) — Pi-specific quiescence, child-wave, and cleanup behavior.
- [Signal cancellation](signal-cancellation.md) — CLI/app cancellation dispatch and scope cleanup.

## Anti-Patterns

- **Don't call `connection.send_user_message()` directly** — always go through `SpawnManager.inject()` so the action coordinator serializes it.
- **Don't observe events before they're persisted** — the drain loop ordering is the persistence guarantee. Breaking it means observers may see events that weren't written to disk.
- **Don't share a manager across concurrent event loops** — the session dict is not thread-safe.

## Related KB

KB lives at `$MERIDIAN_CONTEXT_KB_DIR` (see `meridian context kb`). Use the
codebase guide there for broader streaming module orientation.
