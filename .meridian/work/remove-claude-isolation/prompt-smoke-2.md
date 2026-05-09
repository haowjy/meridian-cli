# Smoke Test: Post-Cleanup Verification

Work in `/home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec/`.

Two commits removed the Claude config overlay isolation system. Verify everything still works.

## Tests

### 1. Spawn without CLAUDE_CONFIG_DIR set
Unset the var and spawn:
```bash
env -u CLAUDE_CONFIG_DIR uv run meridian spawn -m haiku -p "Reply with exactly OK"
```
Verify spawn succeeds and session log is readable.

### 2. Spawn WITH CLAUDE_CONFIG_DIR set (passthrough)
Set it to a real path and spawn:
```bash
CLAUDE_CONFIG_DIR=~/.claude uv run meridian spawn -m haiku -p "Reply with exactly OK"
```
Verify spawn succeeds and session log is readable.

### 3. No overlay dirs exist
```bash
find ~/.meridian/projects -maxdepth 4 -type d -name 'claude-config' -print 2>/dev/null
```
Should return nothing.

### 4. Doctor runs clean
```bash
env -u CLAUDE_CONFIG_DIR uv run meridian doctor
```
Verify:
- No warnings about stale Claude overlays
- No mention of "overlay" in output

### 5. Full test suite
```bash
uv run ruff check .
uv run pyright
uv run pytest-llm
```

### 6. Grep for overlay remnants
```bash
grep -rn "overlay" src/meridian/ --include="*.py" | grep -iv "env_overlay\|runner_overlay\|overlay_env"
```
Report any Claude-overlay-specific hits.

Report all results.
