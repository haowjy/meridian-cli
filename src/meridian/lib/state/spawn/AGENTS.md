# state/spawn/ — Spawn Domain Models and V3 Persistence

Domain types, disk persistence, and pure transitions for per-spawn state files; the
substrate under `spawn_store.py`. Locking, lock order, and the mutation seam:
[../AGENTS.md](../AGENTS.md).

- `model.py` — `SpawnRecord` (starting prompt is reconstructed from `starting-prompt.md`,
  not stored in `state.json`), `TerminalFacts`, `SpawnOrigin`.
- `repository.py` — strict on-disk v3 `StoredSpawnState` and the locked mutation result
  (`Applied` / `Declined` / `Missing`). A persistence leaf; cross-leaf operations belong
  in `spawn_aggregate.py`.
- `legacy.py` — one-shot v2→v3 in-memory upgrade. Reads never rewrite legacy rows; unknown
  or conflicting legacy facts quarantine.
- `transitions.py` — pure transitions; no I/O or locking.
- `dogfood_migration.py` — one-time migration run by `meridian doctor` and primary-launch
  repairs; delete once no dogfood rows remain.

## Rules

- A mutator returns the next `StoredSpawnState` or `Decline(reason)`; decide applicability
  against the locked snapshot, never an unlocked preflight read. Mutators preserve
  `history_id` and `state_revision`; only the locked repository advances the revision.
- Top-level `status` is the sole status authority. `terminal: TerminalFacts | None` carries
  terminal facts (required `exit_code`) without repeating status; terminal statuses require
  it, active and `unknown` rows forbid it. Branch on `record.terminal`.
- The persisted model is `extra="forbid"`; validation failures become `SpawnStateQuarantined`
  reports. Collection reads return `SpawnScan` with separate `records` and `quarantines`;
  callers must handle both. A quarantined file whose `v` is an int above 3 was written by a
  newer build (`newer_format_chat_ids`): the history index skips it with a warning and the chat
  observer limits it to its `current_chat_id`/`chat_id`. Other quarantines fail closed.
- Terminal write authority and applicability are decided inside finalize's locked transition.

[../.context/CONTEXT.md](../.context/CONTEXT.md) — atomic writes, layout, reconciliation.
