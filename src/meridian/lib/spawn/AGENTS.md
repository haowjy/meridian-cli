# lib/spawn/

Spawn archive visibility helpers. Thin module — one file.

## Entry Points

- `archive.py` — `archive_spawn()`, `read_archived_spawns()`,
  `write_archived_spawns()`, `is_spawn_archived()`: manage the
  `~/.meridian/app/archived_spawns.json` visibility file

## Depth Reference

- `.context/CONTEXT.md` — why this exists separately from the state layer,
  locking contract, what "archived" means

## Related

- `../state/` — spawn event store (authoritative spawn records)
- `../ops/spawn/` — operations that call `archive_spawn()`
