# v001: UUID State Split Migration

## What

Moves runtime state from repo-local `.meridian/` to user-level `~/.meridian/projects/<uuid>/`:

| Source | Destination |
|--------|-------------|
| `.meridian/spawns.jsonl` | `~/.meridian/projects/<uuid>/spawns.jsonl` |
| `.meridian/sessions.jsonl` | `~/.meridian/projects/<uuid>/sessions.jsonl` |
| `.meridian/spawns/` | `~/.meridian/projects/<uuid>/spawns/` |

## Why

The UUID-based workspace config (commit `9d3442a`) separates:
- **Repo state** (`.meridian/work/`, `.meridian/fs/` — pre-refactor layout, now replaced by user-level paths) — committed, shareable
- **Runtime state** (spawns, sessions, cache) — per-user, not committed

Existing repos have runtime state in the repo root. This migration moves it to the correct location.

## When to Run

Run this migration if:
- You have an existing repo with `.meridian/spawns.jsonl` or `sessions.jsonl`
- You upgraded to meridian 0.0.34+ (UUID model)
- Running `meridian spawn list` shows no history but you know you have old spawns

## Detection

The check script detects:
1. Repo has `.meridian/spawns.jsonl` or `sessions.jsonl` (legacy state exists)
2. Repo has `.meridian/id` (UUID model is active)
3. Migration hasn't been applied yet

If all three: migration is needed.

## Effects

- Copies (not moves) legacy files to user root
- Original files remain in repo (can be manually deleted after verification)
- Updates `.migrations.json` tracking in both repo and user roots

## Rollback

Not typically needed since originals are preserved. If user root state needs to be cleared:
```bash
rm -rf ~/.meridian/projects/<uuid>/spawns.jsonl
rm -rf ~/.meridian/projects/<uuid>/sessions.jsonl
rm -rf ~/.meridian/projects/<uuid>/spawns/
```
