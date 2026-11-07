# lib/streaming/

The async runtime connects live harnesses to spawn lifecycle, hooks and subscribers.
Keep harness completion policy behind the drain coordinator, not in `SpawnManager` or
the generic drain loop. A native task source or resident backend selects the shared hold;
otherwise Pi selects its own completion profile in `drain_plan_factory.py`. The plan factory injects delivery and descendant
cleanup capabilities into policy rather than letting policy construct application services.

The hold's lifecycle is the pure `decide(state, facts)` table in `hold_state.py` and its
folds. Add behavior there rather than adding flags to the I/O shell. Its rationale is in
[completion and entry points](.context/completion-and-entry-points.md).

The drain normalizes the raw frame, refines/dedupes it, passes refined semantics to the
connection, runs hooks on the raw frame, fans out the normalized event with refined
semantics, calls `note_event_delivered` with the raw frame, then handles the refined
terminal outcome. Hooks must see frames before subscribers; hook failures do not
block delivery. Pass the connection's primary event scope into normalization so child
terminal frames cannot finish or fail the parent. Do not gate hooks or delivery on a
history writer, or reintroduce a persisted runner event stream.

Never call `connection.send_user_message()` directly: use `SpawnManager.deliver()` so
messages and interrupts share the action coordinator's serialization. Spawn-owned journal
writes must use `mutate_published_spawn_artifact()` at write time, including after an
await, so they cannot recreate a deleted spawn.

Terminal publication belongs to `SpawnManager`: `stop_spawn()` must publish even if the
drain task is cancelled. It resolves the outcome, runs the finalizer, resolves the completion
future and sends subscribers the terminal sentinel before scheduling teardown. The caller
awaiting the outcome writes the spawn row. Capture a
connection's PID and scope before stopping it if a later safety pass needs them: stop can
clear them. The stop backstop uses the recorded spawn scope.

The subscriber's terminal `None` sentinel must never be dropped: evict an event if the
queue is full, or the subscriber can hang forever. Do not share a manager across concurrent
event loops; its session registry is not thread-safe.

See [runtime context](.context/CONTEXT.md) for lifecycle and socket contracts and
[Pi drain](.context/pi-drain.md) for Pi quiescence.
