# lib/spawn/ — Spawn Archive Overlay

UI-layer visibility flag for terminal spawns. This is not spawn state — archiving
does not touch the authoritative spawn record in `lib/state/`.

## What "Archived" Means

A user archives a spawn to hide it from lists. The archive is a separate JSON set
(`<runtime_root>/app/archived_spawns.json`) overlaid on top of the state store.
The spawn's `state.json` record is untouched; only the visibility overlay changes.

**Archive is not finalization.** A spawn must already be in a terminal state before
it can be archived — that precondition is enforced at the ops layer
(`SpawnApplicationService.archive()`), not here.

**Archive is one-way.** There is no `unarchive_spawn()`. Once in the set, always in
the set. This is intentional — visibility suppression is permanent.

## Locking Contract

Both `read_archived_spawns()` and `write_archived_spawns()` acquire
`archived_spawns.flock` before reading or writing. Do not access
`archived_spawns.json` without holding this lock. The write path uses
`atomic_write_text()` (tmp+rename) inside the flock. `archive_spawn()` is idempotent
— calling it twice for the same ID leaves the file unchanged.

## Entry Points

- `archive.py` — `archive_spawn()`, `read_archived_spawns()`,
  `write_archived_spawns()`, `is_spawn_archived()`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — why this lives separately from the state
   layer; user-level vs project-level storage distinction.

## Related

- `../state/AGENTS.md` — authoritative spawn records (untouched by archiving)
- `../ops/spawn/AGENTS.md` — enforces terminal-state precondition before calling here
