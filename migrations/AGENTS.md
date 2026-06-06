# Migrations

Standalone scripts that transform on-disk state between schema versions.

## Key Principles

1. **Never import migrations from runtime code.** Migrations import from `meridian.*`, never the reverse.
2. **Check before mutating.** Every migration detects if already applied and no-ops.
3. **Two-phase commit.** Stage to `.migration-staging/`, validate, then atomic commit. Never mark complete before data is safe.
4. **Atomic writes always.** `tmp + fsync + rename`. Never write directly to the target path.

## Auto vs Manual

Declared as `mode` in `registry.toml`:

- **`auto`**: deterministic, lossless, unambiguous, local, reversible → runs at startup
- **`manual`**: conflict-prone, lossy, cross-scope, ambiguous, or destructive → user runs `meridian migrate run vNNN`

## Creating a New Migration

1. Check `registry.toml` for next `vNNN` (3 digits, zero-padded)
2. Create `migrations/vNNN_short_name/` with: `README.md`, `check.py`, `migrate.py`, optional `rollback.py`
3. `check.py` returns JSON: `{"status": "needed"|"done"|"not_applicable", "reason": "..."}`
4. `migrate.py` calls check → backup → stage → validate → commit → update `.migrations.json`
5. Register in `registry.toml`

Read existing migrations for patterns before writing a new one.

## Don't

- Auto-run `mode = "manual"` migrations — ever
- Delete source data until migration is confirmed successful
- Import from `migrations.*` anywhere in `src/meridian/`
- Edit applied migrations — immutable once released
- Write directly to target paths — always stage + atomic commit
