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
  └─ _event_hooks: dict[SpawnId, list[EventHook]]

DrainPlan
  ├─ coordinator: DrainCoordinator | None
  ├─ policy: DrainPolicy
  ├─ aux_wake / handle_aux_wake
  ├─ finalizer
  ├─ teardown: DrainSessionTeardown
  └─ accepted_signals: done/rearm kinds the coordinator consumes

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
- `spawn_drain_loop.py` — drain loop, hook/write/fan-out ordering, outcome priority
- `drain_coordinator.py` — `DrainPlan` plus the narrow `DrainCoordinator` seam
- `drain_teardown.py` — harness-neutral plan-owned async connection-stop contract
- `pi_drain_teardown.py` — Pi cleanup-phase connection-stop policy
- `spawn_session.py` — `SpawnSession` and `DrainOutcome` carriers
- `resident_drain.py` — `HoldEvidence`, shared candidate confirmation/rearm policy, hold builder
- `pi_drain.py` — Pi spawned-session quiescence coordinator
- `pi_lifecycle_tracker.py` — validation for produced Pi lifecycle events
- `disk_watcher.py` / `pi_quiescence.py` — disk-backed Pi quiescence inputs
- `drain_wait.py` — bounded wait helpers for drain/cleanup paths

## Contracts

### Drain Loop Ordering

The drain normalizes each raw frame once, then the coordinator observation seam may
refine or deduplicate that normalized event. Connection-local state receives the
refined semantics. `SpawnManager._run_event_hooks` receives the original raw frame;
for Pi, an `agent_end` frame remains visible there and in the subscriber's raw
envelope. Only its decoded attempt terminal outcome is retained privately until
`agent_settled`, whose refined descriptor carries that outcome. The ordering is:

```text
normalize → coordinator refinement/dedupe
  → receiver semantics (refined)
  → inline hooks (raw frame)
  → subscriber fan-out (raw envelope + refined semantics)
  → note_event_delivered(raw) → terminal handling
```

A hook error is logged and does not block delivery. Meridian persists no runner
event stream.

Terminal classification follows successful delivery. The loop passes the
connection's `primary_event_scope` to the harness semantics: child Codex threads
and OpenCode task sessions reach hooks/subscribers, but cannot complete/fail the
parent, clear its signals, or drive its activity transitions.

### DrainPlan Selection

`drain_plan_factory.build_drain_plan()` returns the whole drain-loop configuration
for one active spawn. `SpawnManager._select_drain_plan()` only supplies manager-owned
capabilities and delegates to it. There are two completion profiles: the shared
resident/native-task hold and Pi. A native task source or resident backend selects
the shared hold. It is built by `ResidentDrainCoordinator.build`; its hold profile
owns candidate-scoped signal admission, reads one `HoldEvidence`, and composes
`RearmWindow` for absolute deadlines and budgeted rearm. Pi keeps its separate
child-wave windows. Claude's connection supplies
`harness/claude_drain.py`'s
`ClaudeNativeTaskSource`; it owns snapshot parsing, not policy, and reports only
blockers and a generation. `HoldEvidence` merges that source with persisted
descendants for every hold: `unknown` descendants dominate, otherwise either source's
blockers hold. Native tasks and the pending follow-up result are diagnostic blockers,
not policy flags carried through assessments.
The connection also declares whether the launched child can write Meridian's completion
signals. `ResidentDrainCoordinator.build` turns that capability into the hold's accepted
signals, which the factory copies into the drain plan. A hold without that channel still
applies the same evidence and deadline policy, but accepts a ready candidate without a
confirmation nudge. Read-only Codex uses this live fallback when the launch lacks spawn
capability, or when the Meridian execpolicy rules file could not be written. Pi RPC gets
`PiDrainCoordinator` plus disk-watcher aux wakes. Plain streaming harnesses
intentionally run with
`DrainPlan(coordinator=None)`. There is no public no-op coordinator class — absence
of a coordinator is the plain path.

Each plan declares `accepted_signals`: hold plans with a completion-signal channel consume
`done` and `rearm`; holds without one consume neither; Pi consumes only `done` (it has no
rearm window and leaves `rearm.signal` alone); and the plain plan consumes neither. Which
plan a spawn gets depends on the live connection, so no table keyed on
the harness id can
know it. `SpawnManager.start_spawn` therefore writes the set to
`spawns/<id>/accepted-signals.json` right after selecting the plan, before the drain
starts, and `meridian spawn done` / `rearm` read it under the published-artifact lock
before writing. It stays off the spawn row because `SpawnStateFields` forbids unknown
keys: 0.11.0 and older would quarantine every row carrying a new field, while they
ignore extra files in the spawn dir. An absent file means the plan isn't chosen yet or
the runner predates the file; the CLI writes the signal and says delivery is
unconfirmed.

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

### Shared Descendant Assessments

Pi and resident completion share one immutable persisted-descendant assessment through
`DescendantRefreshOwner`. Ordinary event handling and readiness/count accessors read the
cache; they never perform discovery. The owner runs at most one blocking assessment in
`asyncio.to_thread()`, starts the next periodic interval when that read finishes, and
coalesces requests received during a read into one immediate follow-up. Stopping the
owner cannot forcibly stop Python's worker thread, so an epoch fence prevents a late
result from mutating the stopped coordinator.

Each assessment catches up the history index once, recursively discovers the transitive
subtree, and authoritatively rereads only selected loose rows. Archived rows remain
traversal edges, allowing a loose descendant below an archived intermediate to be
found. Missing selected state or any discovery/read failure produces `unknown`, never
an empty tree. The warm path is subtree-sized; cold index initialization remains
corpus-sized. See [state history context](../../state/.context/history.md) for the
projection contract.

Every proposed success receives a request sequence and waits for a refresh that covers
that request before policy is reevaluated. A cached `ready` assessment cannot publish
success. The coordinator's `stabilizing` phase has its own policy timer; when it elapses,
the coordinator disarms that timer and enters a distinct `validating` phase while it
awaits the already-requested fresh read. Activity interrupts either phase and
invalidates pending validation. Clearing the retained candidate is a separate
operation. Explicit `done` may override known `blocked` evidence under resident policy;
Pi additionally requires parent idle and no owed result/publication. Neither policy
overrides `unknown`. Refresh completions and other auxiliary or lifecycle wakes
only request reevaluation; they are not completion authority. After event EOF, the same
waiter stops event reads but continues refresh, poll, stabilization, validation, nudge,
and deadline arbitration until the candidate is accepted or rejected.

The retained candidate carries its report text. When a successful terminal turn becomes
the first candidate, the drain loop captures that turn's text
(`AttemptFacts.completion_text`); a granted `rearm` clears it, and the next candidate
captures again. Finalization (`launch/report.py`) uses a `report.md` the child wrote
first, then that captured text, then later assistant text, so a hold child's reply to
the confirmation nudge, written after `done`, is not the report. The hold nudge
(`_HoldProfile._nudge_message`) says so: the previous reply is kept; run
`meridian spawn done` if it is final, otherwise `meridian spawn rearm` before doing more
work. Pi differs: its reply to its own nudge is the report, and
`PI_COMPLETION_NUDGE_MESSAGE` says that instead. Keep each nudge's wording matched to
what its plan retains, and keep report rules out of `launch/spawn_guidance.py`, whose
system-prompt text cannot know the plan. Do not switch to "latest text wins": a child that answers the nudge with
"Done." would replace its real report, and a confirmation turn cannot be told apart from
continued work without harness-specific tool tracking.

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
A synthetic `error/connectionClosed` makes confirmation unavailable: ready evidence
accepts the retained candidate, while persisted blockers or temporarily unknown
evidence remain held. Native-only blockers and non-transport terminal failures keep
their failure diagnostics. A success already recorded for publication is still
protected by the generic success-first barrier.

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
runs the plan finalizer, resolves the completion future, emits lifecycle hooks, and
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
  → fixed per-user POSIX directory / hashed control socket
  → ControlSocketServer._handle_client()
  → SpawnManager.inject()
    → ControlActionCoordinator.run_action()  — journals and serializes the action
    → connection.send_user_message(message)
```

Both the server and inject client derive the same bounded POSIX socket path from
the resolved runtime root and spawn ID. The socket lives in the mode-0700, UID-scoped
directory `/tmp/meridian-<uid>`, independent of either process's `TMPDIR`, so
runtime/project path depth cannot exceed `sockaddr_un.sun_path` and concurrent spawns
remain isolated. Control injection is POSIX-only; the server's legacy native-Windows
branch has no supported client.

Startup sweeps only current-user `control-<32 hex>.sock` files older than 120 seconds.
The age gate covers the short window inside `bind()` between creating the socket
file and attaching the kernel socket.
A nonblocking **datagram** connect must return `ECONNREFUSED`, and the file identity
must still match before unlink, preserving external replacements during the scan. A dead socket file has no bound kernel socket; a
bound stream socket instead rejects a datagram connect with `EPROTOTYPE` on Linux
and macOS, before testing listen/backlog state. This preserves paused runners and endpoints between bind and listen without filling their queues.
A stream probe is unsafe here: macOS can refuse a live listener with a full backlog.
Uncertain errors, symlinks, other owners and other names are left alone.

Each runner holds `spawns/<id>/launch.lock` for its lifetime (see
[launch lock ownership](../../state/launch_lock.py)). `spawn --continue` allocates a
new spawn ID, hence a new socket name; another runner cannot rebind a swept name.
The identity recheck does not replace that invariant. The sweep skips its own endpoint, whose
startup/stop unlink remains owned by that runner. No extra socket registry or lock
files are needed. Recovery runs on the next spawn start, not a timer.

Test isolation crosses interpreter boundaries; see the
[test guidance](../../../../../tests/AGENTS.md).

All control actions (inject, interrupt, permission reply, user input reply) are
serialized through `ControlActionCoordinator`. Concurrent actions queue behind it —
do not call `connection.send_user_message()` directly.

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
- **Do not gate hooks or delivery on writer existence.** Facts and subscribers must work without runner-history persistence.
- **Don't share a manager across concurrent event loops** — the session dict is not thread-safe.

## Related KB

KB lives at `$MERIDIAN_CONTEXT_KB_DIR` (see `meridian context kb`). Use the
codebase guide there for broader streaming module orientation.
