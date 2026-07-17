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

`read_archived_spawns()` takes a shared lock. All writes go through
`mutate_archived_spawns()`, which holds an exclusive, non-reentrant lock across the
complete read-modify-write and publishes with `atomic_write_text()`. The stable lock
inode lives under `<runtime_root>/locks/`, outside the app data directory.
`archive_spawn()` is idempotent and reports whether it newly inserted the ID.

## Entry Points

- `archive.py` — `archive_spawn()`, `read_archived_spawns()`, and the sole write
  seam `mutate_archived_spawns()`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — why this lives separately from the state
   layer; user-level vs project-level storage distinction.

## Related

- `../state/AGENTS.md` — authoritative spawn records (untouched by archiving)
- `../ops/spawn/AGENTS.md` — enforces terminal-state precondition before calling here
