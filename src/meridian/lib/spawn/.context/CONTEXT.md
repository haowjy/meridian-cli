# spawn/ — Spawn Archive Visibility

## What This Is

`archive.py` manages a single JSON file: `<runtime_root>/app/archived_spawns.json`.
This file holds a set of spawn IDs that the user has explicitly archived — a
UI-layer visibility flag, not a state change to the spawn record itself.

**Archiving is not finalization.** A spawn must already be in a terminal state
before it can be archived (enforced at the ops layer in `SpawnApplicationService.archive()`).
The `archived_spawns.json` file is a separate overlay; the spawn's authoritative
record in the state store is untouched.

## Why Separate From the State Layer

The spawn event store (`lib/state/`) owns authoritative spawn records. Archived
status is presentation-only — it controls whether a spawn appears in lists, not
what happened to it. Keeping it in a separate file avoids coupling UI-layer
visibility decisions to the event store's atomic write model.

The overlay lives under `<runtime_root>/app/` — user-level state, not
project-level — because archived visibility is per-user, not per-project.

## Contracts

**Locking.** `read_archived_spawns()` and `write_archived_spawns()` both acquire
`archived_spawns.flock` before reading or writing. Do not read or write
`archived_spawns.json` without holding this lock. The write path uses
`atomic_write_text()` (tmp+rename) inside the flock.

**Idempotent writes.** `archive_spawn()` reads, adds, writes — it is idempotent.
Calling it twice for the same spawn ID leaves the file unchanged on the second call.

**No unarchive.** There is no `unarchive_spawn()`. Once a spawn ID is in the set,
it stays there. This is intentional — archive is a one-way visibility suppression.

## Lateral Links

- `../../ops/.context/CONTEXT.md` — `SpawnApplicationService.archive()` enforces
  terminal-state precondition before calling into this module
- `../../state/` — authoritative spawn records; untouched by archiving
