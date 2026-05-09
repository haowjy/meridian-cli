# Task: Delete All Legacy Claude Overlay References

Work in `/home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec/`.

This worktree is targeting v0.1.0 — no backward compatibility needed. Remove ALL legacy overlay code, not just the machinery. Previous commit `5bf60901` removed the core overlay system but left some vestiges.

## What to fix

### 1. Normalize `CLAUDE_CONFIG_DIR` passthrough

In `src/meridian/lib/harness/claude.py` `prepare_prelaunch()`, the raw `CLAUDE_CONFIG_DIR` string is forwarded without normalization. Fix:
- Compute canonical path with `expanduser().resolve()` 
- Forward the canonical string in `CLAUDE_CONFIG_DIR` env override
- Persist the canonical string to spawn/session metadata via `record_effective_config_dir`
- This prevents continue/fork from failing when `CLAUDE_CONFIG_DIR` was set to `~/foo` or a relative path

### 2. Remove stale overlay scanning from doctor

The doctor still scans for and reports stale Claude overlay directories. Since overlays are fully removed, doctor should stop looking for them entirely.

Find and remove:
- Any `stale_claude_overlays` scanning/reporting in `src/meridian/lib/ops/diag.py`
- The `StaleClaudeOverlay` type/dataclass wherever defined
- The `prune_stale_claude_overlays()` function in `src/meridian/lib/ops/pruning.py` (it's already a no-op, now delete it entirely)
- Any doctor warning text about "stale overlays" being "materialized and pruned with --prune"
- Related test code in `tests/integration/ops/test_diag.py` and `tests/unit/ops/test_pruning.py`

### 3. Remove `claude_config_metadata.py` if still present

Check if `src/meridian/lib/state/claude_config_metadata.py` still exists. If the previous commit already deleted it, skip. If not, delete it now.

### 4. Remove ALL remaining overlay references

Search comprehensively for any remaining references to the old overlay system. Use grep/rg to find:
- `overlay` in the context of Claude config (not other overlays like env overlays which are legitimate)
- `isolated_config`
- `prepare_isolated_claude_config`
- `materialize_overlay`
- `cleanup_claude_overlay`
- `ClaudeOverlay` (any type/class with this prefix)
- `_OVERLAY_METADATA_FILENAME`
- `_TRANSCRIPT_LOCKS_DIRNAME`
- `_SKIP_ENTRIES` / `_COPY_ENTRIES` (the overlay entry classification constants)
- `_classify_overlay_entry`
- `MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV` — evaluate if this is still needed. If nothing uses it anymore, delete it.
- `claude-config` directory references in runtime paths
- References in docs: `docs/troubleshooting.md`, `docs/commands.md`

Delete everything that references the old overlay system. This is v0.1.0 — no legacy compat.

### 5. Clean up `HarnessPrelaunchState`

Check if `HarnessPrelaunchState` still carries overlay-specific fields like `cleanup_overlay_root` or `cleanup_canonical_root`. If those fields are now always `None`/empty, simplify or remove them.

### 6. Remove `update_session_claude_config_dir` if dead

Check if `update_session_claude_config_dir` in session_store is still called anywhere meaningful. If the only callers were the overlay machinery (now deleted), remove the function too.

## Verification

```bash
uv run ruff check .
uv run pyright
uv run pytest-llm
```

Then grep to confirm no remaining overlay references:
```bash
grep -rn "overlay\|isolated_config\|prepare_isolated\|materialize_overlay\|cleanup_claude_overlay\|ClaudeOverlay\|_OVERLAY_METADATA\|_TRANSCRIPT_LOCKS\|_SKIP_ENTRIES\|_COPY_ENTRIES\|_classify_overlay\|MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR" src/meridian/ --include="*.py" | grep -iv "env_overlay\|runner_overlay\|overlay_env"
```

Commit when clean.
