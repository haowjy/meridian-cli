# Task: Remove Claude Session Isolation (Config Dir Copying)

Work in `/home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec/`.

## What to do

Remove the per-spawn Claude config isolation layer. Meridian currently creates an isolated Claude config directory for every Claude spawn (copies credentials, symlinks read-only entries, sets `CLAUDE_CONFIG_DIR` to the overlay, then materializes transcripts back on cleanup). This causes auth corruption bugs and is unnecessary complexity now that Claude Code handles concurrent sessions natively.

**End state**: Passthrough only. If the user already has `CLAUDE_CONFIG_DIR` set in their environment, forward it to spawns. Otherwise don't set it. No overlay, no copy, no isolation.

## Detailed plan

### 1. `src/meridian/lib/harness/claude_preflight.py`

Delete the overlay machinery:
- `prepare_isolated_claude_config()` (L358-408)
- `_classify_overlay_entry()`, `_copy_entry()`, `_link_entry()` helpers
- `materialize_overlay_transcripts()` and `cleanup_claude_overlay()`
- `_SKIP_ENTRIES`, `_COPY_ENTRIES`, `_OVERLAY_METADATA_FILENAME`, `_TRANSCRIPT_LOCKS_DIRNAME` constants
- `_overlay_source_root_and_original_env()`, `resolve_overlay_materialization_canonical_root()`, `resolve_claude_overlay_roots()`
- `_claude_credentials_source()`, `_write_overlay_metadata()`, `_read_overlay_metadata()`
- `ClaudeOverlayRoots`, `ClaudeOverlayCleanupResult`, `ClaudeOverlayMaterializationResult` dataclasses

**Keep** these (still needed for session continue/fork):
- `MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV`
- `ensure_claude_session_accessible()` and `resolve_claude_session_access_source()`
- `project_slug()`
- `_default_canonical_claude_config_root()`, `_claude_config_root()`
- `CLAUDE_PARENT_ALLOWED_TOOLS_FLAG`
- `_dedupe_roots()`, `_resolve_source_session_file()`

Update `__all__` exports to remove deleted names.

### 2. `src/meridian/lib/harness/claude.py` — `ClaudeAdapter`

- `blocked_child_env_vars()` (L425-428): Remove `CLAUDE_CONFIG_DIR` from the blocked set. Keep `CLAUDECODE` (nesting sentinel). Return `frozenset({"CLAUDECODE"})`.
- `prepare_prelaunch()` (L449-512): Gut the overlay setup. Instead:
  - Read user's original `CLAUDE_CONFIG_DIR` from env (passthrough)
  - Still handle session access seeding via `resolve_claude_session_access_source()` / `ensure_claude_session_accessible()`
  - Return `HarnessPrelaunchState` with empty overlay paths (no `cleanup_overlay_root`, no `cleanup_canonical_root` beyond the user's real config root)
  - Don't call `prepare_isolated_claude_config`
  - If user had `CLAUDE_CONFIG_DIR` set, pass it through in env_overrides
- `cleanup_prelaunch()` (L514-542): Remove overlay cleanup. No more `cleanup_claude_overlay()` calls. No more materialization. The method can be simplified to a no-op or just persist metadata if needed.
- Remove imports: `prepare_isolated_claude_config`, `resolve_claude_overlay_roots`, `cleanup_claude_overlay`, and any overlay-related types no longer used

### 3. `src/meridian/lib/ops/spawn/execute.py`

- Delete `_prepare_child_claude_overlay()` (L761-831) entirely
- Delete `_cleanup_child_claude_overlay()` (L860-900+) entirely
- Remove imports: `prepare_isolated_claude_config`, `resolve_claude_overlay_roots`, `resolve_overlay_materialization_canonical_root`, `cleanup_claude_overlay`, `MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV`
- The `prepare_prelaunch` call site at L954+ stays (it delegates to the adapter which is now simplified)

### 4. `src/meridian/lib/ops/pruning.py`

- `prune_stale_claude_overlays()`: Make the body a no-op (return 0). Or remove the function if callers can handle it.
- Remove import of `cleanup_claude_overlay`

### 5. `src/meridian/lib/state/claude_config_metadata.py`

- Delete the file entirely, or gut `persist_durable_claude_config_metadata` to a no-op. Check if anything still imports it first.

### 6. `src/meridian/lib/state/session_store.py`

- Keep `claude_config_dir` field on `SessionRecord`, `SessionStartEvent`, `SessionUpdateEvent` — removing it breaks JSONL event parsing for existing data. Just stop writing meaningful values.

### 7. Tests

Update test files that reference the overlay machinery:
- `tests/unit/state/test_session_store_claude_config_dir.py`
- `tests/unit/ops/test_pruning.py`
- `tests/unit/launch/test_claude_session_access.py` (keep — session access still works)
- `tests/integration/harness/test_adapter_ownership.py`
- `tests/unit/launch/test_nested_claude_deny.py`
- Other test files referencing `claude_config_dir` or overlay

## Implementation order

1. claude_preflight.py — delete overlay machinery
2. claude.py — simplify adapter methods
3. execute.py — delete overlay helpers
4. pruning.py — simplify
5. claude_config_metadata.py — delete or stub
6. Tests — update all affected
7. Verify: `uv run ruff check .` && `uv run pyright` && `uv run pytest-llm`

## Critical constraint

- Do NOT delete `ensure_claude_session_accessible`, `resolve_claude_session_access_source`, or session continue/fork logic — that's separate from isolation
- Do NOT remove `claude_config_dir` fields from session store models — backward compat for existing JSONL
- Do NOT remove `CLAUDECODE` from blocked env vars — that's the nesting sentinel, separate concern
- Commit after verification passes
