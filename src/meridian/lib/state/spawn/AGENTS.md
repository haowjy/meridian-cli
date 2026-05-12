# state/spawn/ — Spawn Domain Models and V2 Persistence

Domain types, disk persistence, and finalization policy for per-spawn state files.
This is the low-level substrate that `spawn_store.py` in the parent package builds on.

## What's Here

Four files with distinct responsibilities:

**`model.py`** — in-memory types: `SpawnRecord`, `LaunchMode`, `SpawnOrigin`.
`SpawnRecord` is what the rest of the system works with; it includes the starting
prompt text (read separately from `starting-prompt.md`, not stored in `state.json`).

**`repository.py`** — `StoredSpawnState` (on-disk v2 schema) and the read/write
helpers. `StoredSpawnState` is Pydantic and excludes the prompt body — prompts can
be large and are stored in a separate file to keep `state.json` lean.

**`transitions.py`** — pure status mutator functions: `apply_mark_running()`,
`apply_mark_finalizing()`, `apply_finalize()`, `apply_record_exited()`. No I/O,
no locking. Takes a `StoredSpawnState`, returns a new one. Callers handle persistence.

**`terminal_policy.py`** — `decide_terminal_write()`: authority lattice for terminal
field writes. A runner-origin terminal write supersedes a reconciler-origin write.
The reaper calls this before finalizing — it will not overwrite a spawn the runner
already terminated with higher authority.

## Two-Tier Write Model

**Tier 1 (owner, unlocked):** The spawn's runner calls `write_state()`. It is the
sole writer while active. Best-effort terminal monotonicity guard — refuses to
overwrite already-terminal state unless `allow_terminal_overwrite=True`.

**Tier 2 (external, locked):** Reaper, cancel, and other external writers call
`write_state_locked()`. Acquires `spawns/<id>/state.lock`, reads current state,
applies a mutator, writes atomically. Skipping the lock races with the owner.

The distinction is convention, not runtime enforcement.

## Key Rules

**Read via `read_state()`, not raw JSON.** Raw reads bypass Pydantic validation
and skip the `starting-prompt.md` reconstruction into `SpawnRecord.starting_prompt`.

**Transitions are pure.** `transitions.py` functions take and return state — they
do not write to disk. The caller decides when and how to persist the result.

**Terminal write authority:** runner supersedes reconciler. `decide_terminal_write()`
returns the action to take; callers must not skip this check.

## Entry Points

- `read_state(spawns_dir, spawn_id)` → `SpawnRecord | None`
- `write_state(spawns_dir, record)` — owner hot path, no lock
- `write_state_locked(spawns_dir, spawn_id, mutator)` — external writers
- `scan_spawn_ids(spawns_dir)` — list spawns with a `state.json`
- `decide_terminal_write(current_status, current_origin, incoming_origin)`

## Depth

→ [.context/CONTEXT.md](../.context/CONTEXT.md) — parent state module: atomic write
   invariants, dual-root layout, read vs write root resolvers, reaper behavior.

## Related

- Parent `../.context/CONTEXT.md` — the two-tier write model is described there
  in full; this subpackage implements the mechanism.
