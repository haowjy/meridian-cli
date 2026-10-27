# Migrations

Standalone, manually run scripts that transform on-disk state between schema versions.
Nothing in `src/meridian/` runs, registers, or imports them, and `meridian migrate` is
the mars passthrough, not this directory. Layout, tracking files, and the script
template: `README.md`; only `v001_uuid_state_split` exists and its registry status is `stub`.

- **Migrations import from `meridian.*`, never the reverse.** Runtime code must not
  import `migrations.*`.
- **Check before mutating.** `check.py` returns `needed`/`done`/`not_applicable`;
  `migrate.py` re-runs `check` first and no-ops unless `needed`.
- **Never delete source data** until the migration is confirmed successful.
- **Applied migrations are immutable** once released; fix forward with a new `vNNN`.
- **Write the target atomically** (tmp + fsync + rename), never in place.
