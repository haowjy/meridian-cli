# Smoke: spawn dry-run

Tests prompt assembly without harness invocation.

## Setup

```bash
. tests/smoke/scripts/setup.sh
smoke_add_agent reviewer
```

## Basic dry-run

```bash
uv run meridian spawn -a reviewer -p "Write hello world" --dry-run --json
```
- [ ] Exit 0
- [ ] `"status": "dry-run"` in JSON
- [ ] `composed_prompt` contains `Write hello world`
- [ ] `model` field present
- [ ] `terminal_surface_mode == "pty_mediated"`

## Model override

```bash
# Write a profile with a declared model, add mars.toml
cat > "$SCRATCH/.mars/agents/reviewer.md" << 'EOF'
---
name: reviewer
model: gpt-5.4
---
# Reviewer
EOF
echo '[settings]' > "$SCRATCH/mars.toml"
echo 'models_cache_ttl_hours = 24' >> "$SCRATCH/mars.toml"

uv run meridian spawn -a reviewer -p "test" -m gpt-5.5 --dry-run --json
```
- [ ] Exit 0
- [ ] `"status": "dry-run"` in JSON
- [ ] `model == "gpt-5.5"`
- [ ] `model_selection.requested_token == "gpt-5.5"`
- [ ] `model_selection.canonical_model_id == "gpt-5.5"`
- [ ] `harness_id == "codex"`
- [ ] `terminal_surface_mode == "pty_mediated"`

## Agent overlay model precedence

```bash
# meridian.toml overlay: gpt-5.4; meridian.local.toml overlay: gpt-5.5
cat > "$SCRATCH/meridian.toml" << 'EOF'
[agents.reviewer]
model = "gpt-5.4"
EOF
cat > "$SCRATCH/meridian.local.toml" << 'EOF'
[agents.reviewer]
model = "gpt-5.5"
EOF

# Without CLI override — local overlay wins
uv run meridian spawn -a reviewer -p "test" --dry-run --json
```
- [ ] Exit 0
- [ ] `model == "gpt-5.5"` (local overlay wins over project overlay)
- [ ] `harness_id == "codex"`

```bash
# With CLI override — CLI wins over all overlays
uv run meridian spawn -a reviewer -p "test" -m gpt-5.4 --dry-run --json
```
- [ ] Exit 0
- [ ] `model == "gpt-5.4"` (CLI flag beats local overlay)

## Template variable substitution

```bash
uv run meridian spawn -a reviewer \
  -p "Review {{FILE_PATH}} for {{CONCERN}}" \
  --prompt-var FILE_PATH=src/main.py \
  --prompt-var CONCERN=security \
  --dry-run --json
```
- [ ] Exit 0
- [ ] `composed_prompt` contains `src/main.py`
- [ ] `composed_prompt` contains `security`
- [ ] `composed_prompt` does NOT contain `{{FILE_PATH}}`
- [ ] `composed_prompt` does NOT contain `{{CONCERN}}`

## Reference files

```bash
REF=$(mktemp)
echo "# Reference" > "$REF"
uv run meridian spawn -a reviewer -p "Review this file" -f "$REF" --dry-run --json
```
- [ ] Exit 0
- [ ] `reference_files` array present, or filename appears somewhere in JSON payload

## Empty prompt — no traceback

```bash
uv run meridian spawn -a reviewer -p "" --dry-run --json
```
- [ ] No `Traceback` in stdout or stderr (may exit 0 or non-zero)

## MERIDIAN_PROJECT_DIR wins over worktree-like cwd

```bash
CANONICAL=$(mktemp -d)
WORKTREE=$(mktemp -d)
mkdir -p "$CANONICAL/.mars/agents"
echo "# Reviewer" > "$CANONICAL/.mars/agents/reviewer.md"
printf 'gitdir: /tmp/fake-worktree-git\n' > "$WORKTREE/.git"

MERIDIAN_PROJECT_DIR=$CANONICAL MERIDIAN_HOME=$(mktemp -d) \
  uv run meridian spawn -a reviewer -p "test" --dry-run --json
# (run from $WORKTREE as cwd)
```
- [ ] Exit 0
- [ ] `resolved_authority.project_root` matches `$CANONICAL` (not the worktree cwd)
- [ ] `resolved_authority.project_root_source == "explicit"`
