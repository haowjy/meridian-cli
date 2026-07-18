# state/spawn/ — Spawn Domain Models and V3 Persistence

Domain types, disk persistence, and pure transitions for per-spawn state files.
This is the low-level substrate that `spawn_store.py` in the parent package builds on.

## What's Here

**`model.py`** — in-memory types: `SpawnRecord`, `RunnerExitFacts`, `TerminalFacts`,
`LaunchMode`, `SpawnOrigin`. `SpawnRecord` includes the starting prompt reconstructed
from `starting-prompt.md`; the prompt is not stored in `state.json`.

**`repository.py`** — `StoredSpawnState` (the strict on-disk v3 schema), validated
read/write helpers, and the locked mutation result protocol. It is a persistence leaf;
cross-leaf aggregate operations belong in the parent `spawn_aggregate.py`.

**`legacy.py`** — the one-shot v2→v3 in-memory upgrade seam. Reads never rewrite
legacy rows; their next locked mutation naturally persists v3. Unknown or conflicting
legacy facts quarantine rather than bypassing the strict stored model.

**`transitions.py`** — pure transitions such as `apply_mark_running()`,
`apply_finalize()`, and `apply_record_exited()`. They do no I/O or locking.

## Locked Mutation Model

All published-state mutations call `write_state_locked()`. The repository acquires
the stable per-spawn lock, re-reads the authoritative record, applies the caller's
transition, and atomically persists it. Its discriminated result is exactly one of:

- `Applied(before, after)`
- `Declined(snapshot, reason)`
- `Missing`

A mutator returns the next `StoredSpawnState` or `Decline(reason)`. Applicability is
therefore decided against the locked snapshot, never by an unlocked preflight read.

## Key Rules

**Status authority is `SpawnStatus` (StrEnum).** One enum in `core/domain.py` defines
every valid status. Lifecycle sets are derived from an exhaustive enum-member map;
transition policy is keyed by enum members. `TerminalSpawnStatus` is checked against
the terminal members at import time.

**Terminal status is stored once.** Top-level `status` is the sole status authority.
`terminal: TerminalFacts | None` carries terminal facts, including the required
`exit_code`, but does not repeat status. Terminal statuses require terminal facts;
active and `unknown` rows forbid them. Consumers branch on `record.terminal` rather
than flattened compatibility properties.

**Out-of-vocabulary and extra fields are quarantined, not coerced.** The persisted
model uses `extra="forbid"`; Pydantic validation failures become structured
`SpawnStateQuarantined` reports. Collection reads return immutable `SpawnScan`
envelopes from `spawn_store.py`, with separate `records` and `quarantines` tuples.
Callers must explicitly choose or preserve both partitions.

**Read via `read_state()`, not raw JSON.** Raw reads bypass Pydantic validation and
skip `starting-prompt.md` reconstruction.

**Transitions are pure.** The store owns persistence and terminal-write authority.
Finalize decides authority and applicability inside its locked transition.

## Entry Points

- `read_state(spawns_dir, spawn_id)` → `SpawnRecord | None`
- `write_state_locked(spawns_dir, spawn_id, mutator)` → `LockedMutationResult`
- `scan_spawn_ids(spawns_dir)` — candidate directories containing `state.json`

## Depth

→ [.context/CONTEXT.md](../.context/CONTEXT.md) — parent state module: atomic-write
invariants, dual-root layout, read/write root resolution, and reaper behavior.
