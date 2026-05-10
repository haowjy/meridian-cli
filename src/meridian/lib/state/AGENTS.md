# lib/state/ — State Layer

File-backed authority for all Meridian runtime state. Path resolution, spawn store,
session store, event stores, atomic writes, and orphan reaping. No database, no
service, no in-memory objects that survive process death.

## Entry Points

- **`user_paths.py`** — `get_user_home()`, `get_or_create_project_id()`,
  `get_project_home()`. Cross-platform user-level root resolution. Start here for
  any feature that needs user-level storage.
- **`paths.py`** — `RuntimePaths`, `resolve_project_runtime_root()`,
  `resolve_project_runtime_root_for_write()`. Maps project UUID to concrete filesystem
  paths. Use `*_for_write()` variant only on write paths — read paths must not
  create the UUID.
- **`spawn_store.py`** — `SpawnStore`. The main interface for listing, creating,
  updating, and finalizing spawns. Wraps v2 per-spawn state files.
- **`session_store.py`** — Session event log: record, update, close, and query
  harness sessions.
- **`reaper.py`** — `reap_spawns()`. Auto-finalizes orphaned spawns by checking
  process liveness and heartbeat age. Runs on every read path at root depth only.
- **`atomic.py`** — `atomic_write_text()`, `atomic_write_bytes()`,
  `append_text_line()`. All state writes go through these primitives — never write
  state files with `open()` directly.

## Spawn Subpackage

- **`spawn/model.py`** — `SpawnRecord` projection (in-memory representation)
- **`spawn/repository.py`** — `StoredSpawnState` (on-disk v2 schema), `read_state()`,
  `write_state()`, `write_state_locked()`, `scan_spawn_ids()`
- **`spawn/transitions.py`** — pure status transition functions (`apply_mark_running()`,
  `apply_mark_finalizing()`, `apply_finalize()`, `apply_record_exited()`)
- **`spawn/terminal_policy.py`** — `decide_terminal_write()` projection authority rule

## Other Files

| File | Purpose |
|---|---|
| `event_store.py` | JSONL append store with flock serialization |
| `artifact_store.py` | Blob store for spawn output artifacts (`history.jsonl`, etc.) |
| `history.py` | Spawn history access (replay and streaming read) |
| `work_store.py` | Work item store (one mutable JSON per item) |
| `liveness.py` | `is_process_alive()` — PID + start-time guard against PID reuse |
| `launch_boundary.py` | `LaunchBoundarySummary` — heartbeat + artifact mtime snapshot for reaper |
| `managed_primary.py` | Managed-primary lifecycle state (Codex app-server) |
| `primary_meta.py` | Primary session metadata persistence |
| `process_scope_projection.py` | `MERIDIAN_DEPTH` scope tracking |
| `wordgen.py` / `wordlists.py` | Three-word project ID generation |

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — dual-root layout, v2 spawn state
   format, two-tier write model, read vs write resolver contract, reaper liveness
   sequence, atomic write invariants.

## Related

- `../harness/` — `ArtifactStore` protocol consumers that read from this layer
- `../launch/` — writes spawn state via `SpawnStore` during the launch pipeline
- `../core/spawn_lifecycle.py` — status machine and terminal state helpers used
  by both `spawn_store.py` and `reaper.py`
