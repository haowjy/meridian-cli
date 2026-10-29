# lib/streaming/

The async backbone between a live harness connection and the rest of the system.
`SpawnManager` holds the registry of every active spawn; the drain loop is the
core mechanism that moves events from the harness outward to persistence, observers,
and the subscriber queue.

Mostly mechanism. Generic terminal classification lives in `DrainPolicy`; shared
candidate/deadline mechanics and the stabilization/validation phases live in
`CompletionCoordinator`, behind
the outer `DrainCoordinator` seam. Plain streaming harnesses intentionally run with
`coordinator=None`; Claude/Codex/OpenCode completion shares one hold profile, and Pi
supplies its own profile. Keep Pi child-wave,
disk-state, direct-follow-up gating, candidate confirmation/rearm, and descendant cleanup policy
behind the coordinator/tracker modules instead of growing `SpawnManager`.

`drain_plan_factory.py` is the composition root for plan selection. It owns the
plain/resident/Pi choice and injects manager capabilities for event emission,
serialized injection, and application-service descendant cleanup into both Pi and
resident policy. Drain collaborators must not construct application services.

**Do not key resident behavior off harness ids.** A connection exposes a native task
source (`native_task_source()`) independently of resident backend control. Both feed
one `HoldEvidence(descendants, native)` built by `ResidentDrainCoordinator.build`:
resident backend control nudges through `begin_followup_turn`; Claude supplies its
native task source and is nudged through serialized inject. Every hold also reads
persisted descendants. Select the profile once from these capabilities; evidence
reports facts, while profiles own hold, done/rearm, deadline and nudge policy.

## Completion

Hold completion uses an absolute deadline per arm; only an explicit `rearm.signal` grants
a new window, never ordinary harness or descendant activity. `--timeout` /
`MERIDIAN_TIMEOUT` is the opt-in absolute ceiling (outer attempt timer in
`streaming_runner.py`); rearms and Pi waves cannot reset it. Pi completion is
descendant-quiescence-driven with no default wall-clock bound, by design. Models, rearm
budget, no-signal holds:
[.context/completion-and-entry-points.md](.context/completion-and-entry-points.md).

## Mental Model

```
HarnessConnection  →  drain loop  →  0. normalize + coordinator refinement/dedupe
                                     1. connection semantics (refined)
                                     2. inline hooks (raw event)
                                     3. fan-out (raw envelope + refined semantics)
                                     4. note_event_delivered (raw event)
                                     5. terminal handling
```

The drain normalizes each frame once; a coordinator may refine or deduplicate that
normalized event before delivery. Connection-local state receives the refined
semantics. `SpawnManager._run_event_hooks` then sees the original raw frame (including
Pi `agent_end`), while subscribers receive its raw envelope carrying the refined
semantics. The coordinator's `note_event_delivered` receives the raw frame. A hook
error is logged and does not block delivery. Terminal classification follows successful delivery. See [.context/CONTEXT.md](.context/CONTEXT.md).

`SpawnManager` is the integration point for everything that touches a live spawn:
starting, stopping, injecting messages, and subscribing to events.
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

**Spawn-owned journals share the spawn row's lifetime.** Permission and control-action
writes route through `mutate_published_spawn_artifact()` at write time, including after
awaited dispatches. Never append directly after an async boundary.

**Terminal publication belongs to `SpawnManager`.** The drain loop supplies its
classification, but `stop_spawn()` must still publish the finalized lifecycle row,
resolve completion, and start that spawn's cleanup when a bounded drain is cancelled.

**Drain loop ordering is fixed** (diagram above). Hooks see each delivered frame before any subscriber. Don't reintroduce a persisted runner event stream.

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

`DrainPlan` carries coordinator, policy, aux wake, finalizer and session teardown for one
spawn (`coordinator=None` is the plain path). `SpawnManager.start_spawn` writes the plan's
`accepted_signals` once to `spawns/<id>/accepted-signals.json` before the drain starts, and
the CLI refuses a signal the file doesn't list; don't move it onto the spawn row.
`SingleTurnDrainPolicy` (default) terminates on any terminal event; `PersistentDrainPolicy`
(`start_spawn(drain_policy=...)`) emits `meridian/turn_completed` on success and continues,
terminating on error/cancel.

## Entry Points

`spawn_manager.py` (`SpawnManager`, the public live-spawn API), `spawn_drain_loop.py`
(ordering and outcome classification), `drain_plan_factory.py` (plan selection),
`drain_coordinator.py` (protocol seam), `drain_policy.py`. Full module inventory:
[.context/completion-and-entry-points.md](.context/completion-and-entry-points.md).

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — generic drain runtime and lifecycle
→ [.context/pi-drain.md](.context/pi-drain.md) — Pi quiescence and child-wave behavior

## Related

- `../harness/connections/` — `HarnessConnection` protocol (event source)
