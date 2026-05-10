# lib/chat — Contracts and Architecture

## Five-Layer Model

```
Layer 0: Wire protocol (inside connection class — never exposed upstream)
Layer 1: SpawnManager + HarnessConnection → history.jsonl + observer dispatch
Layer 2: Normalization (HarnessEvent → ChatEvent)
Layer 3: Persistence (events.jsonl → events.db SQLite index)
Layer 4: Delivery (WebSocket fan-out | REST | ReplayService)
```

Wire-protocol details never leave the connection class. All consumers see
`HarnessEvent` at layer 1 and `ChatEvent` at layer 2 and above.

## Ownership Boundaries

**SpawnManager owns the connection.** It runs the drain loop, persists raw
events to `history.jsonl`, and dispatches through `EventObserverRegistry`.
It does not produce `ChatEvent`s.

**ChatEventPipeline is persistence-first.** Events are written to
`events.jsonl` before any fan-out or callback. No event can be delivered to
a WebSocket before it is durable.

**ChatRuntime owns session lifecycle.** It maintains live and
persisted-only chat registries, dispatches commands, and runs close postwork.

**Normalization is a chat concern.** `chat/normalization/` owns the
`HarnessEvent → ChatEvent` projection. The harness layer stops at raw
events and runtime semantics.

## ColdSpawnAcquisition — Observer-Before-Spawn Invariant

`ColdSpawnAcquisition` attaches the `EventObserverRegistry` to `SpawnManager`
**before** the underlying spawn process is created. This prevents a race
where events fire before the observer is registered.

Callers must not attempt to inject the observer after spawn creation.
The acquisition boundary is the `ChatSessionService.send_first_prompt()`
call — acquisition must complete before that call returns.

## ChatRuntime Registries

`ChatRuntime` maintains two registries:
- **live**: chats with an active backend connection; receive and emit events
- **persisted_only**: closed chats whose events are queryable but no new
  events arrive

A chat moves from live to persisted_only after `close` command postwork
completes. Recovery at startup may resurrect a persisted_only chat to
live if its backend process is still running.

## Crash Recovery — 7-Point Contract

Recovery (`recovery.py`) must satisfy:
1. No events are lost that were written to `events.jsonl`
2. No events are duplicated on reconnect
3. `ReplayService` delivers from `last_seq` (exclusive) on reconnect
4. A closed backend process results in a `session.closed` synthetic event
5. An orphaned spawn is detected and finalized
6. Recovery is idempotent — running it twice produces the same result
7. Recovery does not require a live harness process

## Normalization Registry

`normalization/registry.py` maps `HarnessId → Normalizer`. New harness
support = new normalizer class + registration. Normalizers must not
retain state across events — they receive one `HarnessEvent` and emit
zero or more `ChatEvent`s.

## Related KB

→ [KB: architecture/chat/overview.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/chat/overview.md)
→ [KB: decisions/chat-backend.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/decisions/chat-backend.md)

## Lateral Links

→ [../../streaming/.context/CONTEXT.md](../../streaming/.context/CONTEXT.md) — SpawnManager, drain loop, observer registry
