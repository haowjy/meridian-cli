# state/spawn/ — Spawn Domain Models and V2 Persistence

Domain types, disk persistence, and finalization policy for per-spawn state files.
This is the low-level substrate that `spawn_store.py` in the parent package builds on.

## What's Here

Four files with distinct responsibilities:

**`model.py`** — in-memory types: `SpawnRecord`, `RunnerExitFacts`, `TerminalFacts`,
`LaunchMode`, `SpawnOrigin`.
`SpawnRecord` is what the rest of the system works with; it includes the starting
prompt text (read separately from `starting-prompt.md`, not stored in `state.json`).

**`repository.py`** — `StoredSpawnState` (on-disk v2 schema) and the read/write
helpers. `StoredSpawnState` is Pydantic and excludes the prompt body — prompts can
be large and are stored in a separate file to keep `state.json` lean.
It is a persistence leaf and does not compose process-scope projection operations;
cross-leaf spawn aggregate operations live in the parent `spawn_aggregate.py`.

**`transitions.py`** — pure status mutator functions: `apply_mark_running()`,
`apply_mark_finalizing()`, `apply_finalize()`, `apply_record_exited()`. No I/O,
no locking. Takes a `StoredSpawnState`, returns a new one. Callers handle persistence.

**`terminal_policy.py`** — `decide_terminal_write()`: authority lattice for terminal
field writes. A runner-origin terminal write supersedes a reconciler-origin write.
The reaper calls this before finalizing — it will not overwrite a spawn the runner
already terminated with higher authority.

## Locked Mutation Model

All published-state mutations call `write_state_locked()`. The repository acquires
the stable per-spawn lock, re-reads the authoritative record, applies the caller's
pure transition, and atomically persists the result. Mutators return either the
updated `SpawnRecord` or `Decline(reason)`; the repository returns a
`MutationOutcome` containing the authoritative snapshot and whether it wrote.

## Key Rules

**Status authority is `SpawnStatus` (StrEnum).** One enum in `core/domain.py`
defines every valid status. Lifecycle sets (`ALL_SPAWN_STATUSES`,
`ACTIVE_SPAWN_STATUSES`, `TERMINAL_SPAWN_STATUSES`) are derived from a
member-to-lifecycle-class map, not declared as separate constants.
`TerminalSpawnStatus` is a type alias checked against the enum at import time.

**Lifecycle facts are discriminated and atomic.** Runner-exit evidence lives in
`runner_exit: RunnerExitFacts | None`; finalized facts live in
`terminal: TerminalFacts | None`. A terminal top-level status requires matching
`terminal` facts with `terminal.status == status`; active and `unknown` rows
must not carry them. `_RevalidatedFrozenModel.model_copy(update=)` revalidates
the invariant so it cannot be bypassed by in-memory copy.

**Out-of-vocab rows are quarantined, not coerced.** Single reads raise
`SpawnStateQuarantined`; collection reads partition into `SpawnCollection`
(valid rows + quarantine reports). Migration and retention fail closed on
quarantine.

**Read via `read_state()`, not raw JSON.** Raw reads bypass Pydantic validation
and skip the `starting-prompt.md` reconstruction into `SpawnRecord.starting_prompt`.

**Transitions are pure.** `transitions.py` functions take and return state — they
do not write to disk. The caller decides when and how to persist the result.

**Terminal write authority:** runner supersedes reconciler. `decide_terminal_write()`
returns the action to take; callers must not skip this check.

## Entry Points

- `read_state(spawns_dir, spawn_id)` → `SpawnRecord | None`
- `write_state_locked(spawns_dir, spawn_id, mutator)` → `MutationOutcome`
- `scan_spawn_ids(spawns_dir)` — list spawns with a `state.json`
- `decide_terminal_write(current_status, current_origin, incoming_origin)`

## Depth

→ [.context/CONTEXT.md](../.context/CONTEXT.md) — parent state module: atomic write
   invariants, dual-root layout, read vs write root resolvers, reaper behavior.

## Related

- Parent `../.context/CONTEXT.md` — the locked mutation model is described there
  in full; this subpackage implements the mechanism.
