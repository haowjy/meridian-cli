# Review: Remove Claude Config Overlay Isolation

Review commit `5bf60901` on branch `wt/arch/p1a-seam` in `/home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec/`.

## What changed

Removed the per-spawn Claude config isolation layer — the system that created isolated `CLAUDE_CONFIG_DIR` directories per spawn, copied credentials/settings into them, and materialized transcripts back on cleanup. ~2,200 lines deleted.

**End state**: `CLAUDE_CONFIG_DIR` is passthrough-only (forwarded if user sets it, otherwise not touched). No overlay, no copy, no isolation.

## Why

1. Auth corruption: copied credentials raced with concurrent OAuth refreshes causing login loops/401s
2. Complexity: ~400 lines of overlay machinery in claude_preflight.py plus call sites everywhere
3. Claude Code now handles concurrent sessions natively (May 2026 fixes)

## Review focus

1. **Completeness**: Are there any remaining references to the deleted functions/types that would cause runtime errors? Any dead imports?
2. **Session continue/fork**: `ensure_claude_session_accessible` and `resolve_claude_session_access_source` were preserved — verify they still work correctly without the overlay (they should resolve against the user's real `~/.claude/` config)
3. **Session log reading**: `resolve_session_file()` in the Claude adapter reads from `_claude_config_root()` — confirm this still works correctly when `CLAUDE_CONFIG_DIR` is passthrough
4. **Blocked env vars**: `CLAUDE_CONFIG_DIR` was removed from `blocked_child_env_vars()`. Is this correct? Previously it was blocked so the parent's overlay dir wouldn't leak to children. Now there's no overlay, so passthrough is correct.
5. **Pruning**: `prune_stale_claude_overlays()` is now a no-op returning 0. Should it be deleted entirely instead?
6. **Test coverage**: Were overlay-specific tests properly removed/updated? Are there gaps where new tests are needed for the passthrough behavior?
7. **Edge cases**: What happens if `CLAUDE_CONFIG_DIR` is set to a nonexistent path? What if multiple spawns write to the same `~/.claude/projects/` concurrently?

Use `git diff HEAD~1` to see the full diff. Read the modified files to check context around changes.
