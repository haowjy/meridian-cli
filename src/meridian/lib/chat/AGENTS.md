# lib/chat/

Five-layer pipeline from harness wire events to a persistent, multi-client
WebSocket event stream. Backs `meridian chat` and the browser UI.

## Entry Points

- `runtime.py` — `ChatRuntime`: top-level registry and lifecycle owner. Start here.
- `session_service.py` — `ChatSessionService`: per-session state machine (`idle → active → draining → closed`)
- `backend_acquisition.py` — `ColdSpawnAcquisition`: deferred backend attachment on first prompt
- `event_pipeline.py` — `ChatEventPipeline`: persist → index → fan-out → callbacks
- `server.py` — FastAPI app, WebSocket `/ws/chat/{id}`, REST endpoints, `configure()`
- `protocol.py` — `ChatEvent` envelope and event family constants
- `event_log.py` — `ChatEventLog` (JSONL source of truth)
- `event_index.py` — `ChatEventIndex` (SQLite derived index)
- `replay.py` — `ReplayService`: reconnect from `last_seq`
- `recovery.py` — crash recovery and 7-point contract

## Submodules

- `normalization/` — per-harness `HarnessEvent → ChatEvent` projection
  (`claude.py`, `codex.py`, `opencode.py`, `registry.py`)
- `dev_frontend/` — portless Vite launcher and supervisor

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Five-layer model and ownership boundaries
- Persistence-first pipeline invariant
- `ColdSpawnAcquisition` observer-before-spawn contract
- `ChatRuntime` live vs persisted-only registries
- Normalization placement rationale
- Crash recovery contract

## Related

- `../streaming/spawn_manager.py` — `SpawnManager` (drain loop, observer registry)
- `../state/` — `spawns.jsonl` and spawn store
- KB: [architecture/chat/overview.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/chat/overview.md)
