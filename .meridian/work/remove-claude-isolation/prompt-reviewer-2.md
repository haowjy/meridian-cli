# Review: Final State After Claude Overlay Removal

Review the current state of branch `wt/arch/p1a-seam` in `/home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec/`. Two commits removed the Claude config overlay isolation:
- `5bf60901` — Core overlay removal
- `d32ebc29` — Legacy vestiges cleanup + config dir normalization

## Review focus

### 1. Dead/useless tests

The primary focus. Look for tests that:
- Test overlay behavior that no longer exists (isolated config dirs, credential copying, transcript materialization, overlay cleanup)
- Assert `claude_config_dir` values that are no longer written
- Mock or patch functions that were deleted (`prepare_isolated_claude_config`, `cleanup_claude_overlay`, `materialize_overlay_transcripts`, etc.)
- Test `prune_stale_claude_overlays` or `StaleClaudeOverlay` or doctor overlay scanning
- Are now trivial no-ops because the code they tested was removed

Check these files specifically:
- `tests/unit/state/test_session_store_claude_config_dir.py` — is this still testing anything meaningful?
- `tests/unit/ops/test_pruning.py` — any remaining overlay test stubs?
- `tests/integration/harness/test_claude_session_symlink.py` — this was heavily gutted, is what remains useful?
- `tests/integration/launch/test_launch_process.py` — overlay-related test cases removed?
- `tests/integration/ops/test_diag.py` — overlay doctor tests removed?
- `tests/unit/launch/test_nested_claude_deny.py` — still relevant?
- `tests/integration/ops/test_reference.py` — any overlay refs?

### 2. Remaining dead code

Search for any remaining references to the old overlay system that the cleanup missed:
```bash
grep -rn "overlay\|isolated_config\|prepare_isolated\|materialize_overlay\|cleanup_claude_overlay\|ClaudeOverlay\|_OVERLAY_METADATA\|_TRANSCRIPT_LOCKS\|_SKIP_ENTRIES\|_COPY_ENTRIES\|_classify_overlay\|MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR" src/ tests/ docs/ --include="*.py" --include="*.md" | grep -iv "env_overlay\|runner_overlay\|overlay_env\|prompt-"
```

### 3. Config dir normalization

Verify the `CLAUDE_CONFIG_DIR` passthrough normalization in `prepare_prelaunch()` is correct:
- Does `expanduser().resolve()` handle all edge cases?
- Is the normalized value consistently used everywhere downstream?

### 4. `HarnessPrelaunchState` cleanup

Verify `cleanup_overlay_root` and `cleanup_canonical_root` fields are fully removed from:
- The dataclass definition
- All construction sites
- All access sites

Report findings with file paths, line numbers, and severity.
