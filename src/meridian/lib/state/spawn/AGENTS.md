# state/spawn/

Spawn domain models, v2 persistence, and finalization policy for spawn state
files under `~/.meridian/projects/<id>/spawns/<spawn_id>/`.

## Files

- `model.py` — shared domain types: `SpawnRecord`, `LaunchMode`, `SpawnOrigin`
- `repository.py` — `StoredSpawnState`, read/write helpers (`read_state`, `write_state`,
  `write_state_locked`, `scan_spawn_ids`)
- `transitions.py` — pure state mutators (`apply_mark_running`, `apply_finalize`, etc.)
- `terminal_policy.py` — `decide_terminal_write()` authority lattice

## Entry Points

- `read_state(spawns_dir, spawn_id)` — read one spawn; returns `SpawnRecord | None`
- `write_state(spawns_dir, record)` — owner hot path, no lock acquired
- `write_state_locked(spawns_dir, spawn_id, mutator)` — external writers (reaper, cancel)
- `scan_spawn_ids(spawns_dir)` — list all spawns with a `state.json`
- `decide_terminal_write(current_status, current_origin, incoming_origin)` — whether to
  write terminal fields when an event arrives

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Two-tier write model (owner unlocked vs external locked)
- `SpawnRecord` vs `StoredSpawnState` split and why prompt is stored separately
- Terminal write authority lattice (runner supersedes reconciler)
- Why `transitions.py` functions are pure (no persistence or locking)

## Related

- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — parent state module contracts,
  including atomic write rules and read vs write root resolvers
