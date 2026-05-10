# lib/chat/ — Persistent Multi-Client Event Stream

Translates harness wire events into a durable, replayable WebSocket stream. Backs `meridian chat` and the browser UI. The central invariant: events are written to disk before they're delivered anywhere.

## Mental Model

Five layers, each with a clear ownership boundary:

```
Layer 0: Wire protocol     (inside connection class — never exposed upstream)
Layer 1: SpawnManager      → history.jsonl + observer dispatch
Layer 2: Normalization     → HarnessEvent → ChatEvent   (chat/normalization/)
Layer 3: Persistence       → events.jsonl + SQLite index
Layer 4: Delivery          → WebSocket fan-out | REST | ReplayService
```

`ChatRuntime` owns session lifecycle across all layers. `ChatEventPipeline` is the persistence-first bottleneck at layer 3 — no event reaches layer 4 before it's durable.

## Key Rules

- **Persistence-first.** Events write to `events.jsonl` before any fan-out or callback. Violating this breaks crash recovery — a delivered-but-not-persisted event is lost on restart.
- **Observer before spawn.** `ColdSpawnAcquisition` attaches the `EventObserverRegistry` to `SpawnManager` before the underlying spawn process is created. Never inject the observer after spawn creation — there's a race window where events fire before the observer registers.
- **`ChatRuntime` owns two registries.** `live`: chats with an active backend connection. `persisted_only`: closed chats whose events are queryable. A chat moves live→persisted_only after close postwork. Recovery at startup may resurrect a persisted_only chat to live.
- **Normalizers are stateless across events but stateful per execution.** Call `reset()` at execution boundaries (reconnect, reacquire). The harness layer must not import from `chat/normalization/`.
- **Recovery is a 7-point contract** (see `.context/`). The short version: no events lost that hit `events.jsonl`, no duplicates on reconnect, idempotent when run twice, works without a live harness process.

## Entry Points

- `runtime.py` — `ChatRuntime`: start here; top-level lifecycle and registry owner
- `session_service.py` — `ChatSessionService`: per-session state machine (idle → active → draining → closed)
- `backend_acquisition.py` — `ColdSpawnAcquisition`: deferred backend attachment on first prompt
- `event_pipeline.py` — `ChatEventPipeline`: persist → index → fan-out
- `server.py` — FastAPI app, WebSocket `/ws/chat/{id}`, REST endpoints, `configure()`
- `protocol.py` — `ChatEvent` envelope and event family constants

## Submodules

- `normalization/` — per-harness `HarnessEvent → ChatEvent` translation
- `dev_frontend/` — Vite dev server launcher for `meridian chat --dev`

## Anti-Patterns

- Don't deliver events to WebSocket clients before writing to `events.jsonl`. Persistence is not optional.
- Don't inject the observer after spawn creation — attach it during `ColdSpawnAcquisition`, before `send_first_prompt()` returns.
- Don't add normalization logic outside `chat/normalization/` — the harness layer stops at raw events.

## Related

- `../streaming/spawn_manager.py` — `SpawnManager` (drain loop, observer registry)
- `../state/` — `spawns.jsonl` and spawn store

→ [.context/CONTEXT.md](.context/CONTEXT.md) — five-layer model, ownership boundaries, ColdSpawnAcquisition contract, crash recovery 7-point contract
→ [KB: architecture/chat/overview.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/chat/overview.md)
