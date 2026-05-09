# Smoke Test: Claude Spawn Without Config Isolation

Work in `/home/jimyao/gitrepos/meridian-cli.worktrees/arch-exec/`.

## Context

We removed the per-spawn Claude config isolation layer. Previously meridian created an isolated `CLAUDE_CONFIG_DIR` per spawn. Now it's passthrough-only.

## Tests to run

### 1. Basic spawn works without overlay
Spawn a cheap Claude model and verify it completes successfully:
```bash
uv run meridian spawn -m haiku -p "Reply with exactly OK"
```
Verify:
- Spawn succeeds
- No `claude-config/<spawn_id>/` directory created under the runtime root
- Session log is readable: `meridian session log <spawn_id>`

### 2. Check no overlay directories are created
After the spawn, check:
```bash
ls ~/.meridian/projects/*/runtime/claude-config/ 2>/dev/null
```
Should be empty or not exist (no new overlay dirs).

### 3. Verify ruff + pyright + tests still pass
```bash
uv run ruff check .
uv run pyright
uv run pytest-llm
```

### 4. Check env vars
Spawn with debug and verify `CLAUDE_CONFIG_DIR` is NOT set in child env (unless user had it set):
```bash
uv run meridian spawn -m haiku -p "Reply with OK" --debug 2>&1 | grep -i "claude_config_dir"
```

### 5. Session log reading
After spawn completes, verify transcript is accessible:
```bash
meridian session log <spawn_id>
```

Report what passed, what failed, and any unexpected behavior.
