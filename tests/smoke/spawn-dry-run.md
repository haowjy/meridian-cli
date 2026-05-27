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

## Agent routing override precedence (mars.toml `[agents.<name>]`)

```bash
# mars routing config for this project
cat > "$SCRATCH/mars.toml" << 'EOF'
[settings]
targets = [".claude", ".codex", ".opencode"]

[agents.reviewer]
model = "gpt-5.5"
EOF

# Without CLI override — project routing override wins
uv run meridian spawn -a reviewer -p "test" --dry-run --json
```
- [ ] Exit 0
- [ ] `model == "gpt-5.5"` (project `mars.toml` agent override applied)
- [ ] `harness_id == "codex"`

```bash
# With CLI override — CLI wins over agent routing override
uv run meridian spawn -a reviewer -p "test" -m gpt-5.4 --dry-run --json
```
- [ ] Exit 0
- [ ] `model == "gpt-5.4"` (CLI flag beats `[agents.reviewer]` model)

## Mars bundle round-trip + provenance fields

```bash
cat > "$SCRATCH/.mars/agents/reviewer.md" << 'EOF'
---
name: reviewer
model: gpt-5.4-mini
model-policies:
  - match: { alias: gpt55 }
    override: { harness: opencode, effort: medium }
---
# Reviewer
EOF

cat > "$SCRATCH/mars.toml" << 'EOF'
[settings]
targets = [".claude", ".codex", ".opencode"]
EOF

uv run meridian spawn -a reviewer -m gpt55 -p "bundle policy check" --dry-run --json
```
- [ ] Exit 0
- [ ] `status == "dry-run"`
- [ ] `harness_id == "opencode"` (bundle resolved route)
- [ ] `cli_command` includes `--variant` and `medium` (effort projection)
- [ ] `model_selection.requested_token == "gpt55"`
- [ ] `model_selection.canonical_model_id` present
- [ ] `model_selection.harness_provenance` present

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

## Task CWD reporting + kb: path

```bash
KB_ROOT=$(uv run meridian context --json | uv run python -c 'import json,sys; print(json.load(sys.stdin)["kb_resolved"])')
mkdir -p "$KB_ROOT/domain"
echo "kb ref" > "$KB_ROOT/domain/page.md"
uv run meridian spawn -a reviewer -p "Use kb ref" -f kb:domain/page.md --dry-run --json
```
- [ ] Exit 0
- [ ] JSON includes `task_cwd`, `reference_anchor`, `task_cwd_source`
- [ ] JSON omits `authority_root`
- [ ] `task_cwd_source == "authority-root"` in default no-task-dir case

## --work uses work-item task_dir for relative refs

```bash
TASK_DIR=$(mktemp -d)
echo "relative ref" > "$TASK_DIR/notes.md"
uv run meridian work start smoke-task-dir --task-dir "$TASK_DIR"
uv run meridian spawn -a reviewer -p "use relative ref" --work smoke-task-dir -f notes.md --dry-run --json
```
- [ ] Exit 0
- [ ] `task_cwd` equals `$TASK_DIR`
- [ ] `reference_anchor` equals `$TASK_DIR`
- [ ] `task_cwd_source == "explicit-work-task-dir"`
- [ ] `task_cwd_work_item == "smoke-task-dir"`
- [ ] resolved `reference_files` entry points at `$TASK_DIR/notes.md`

## --task-dir overrides work-item task_dir

```bash
OVERRIDE_DIR=$(mktemp -d)
echo "override ref" > "$OVERRIDE_DIR/override.md"
uv run meridian spawn -a reviewer -p "prefer explicit task dir" \
  --work smoke-task-dir --task-dir "$OVERRIDE_DIR" -f override.md --dry-run --json
```
- [ ] Exit 0
- [ ] `task_cwd` equals `$OVERRIDE_DIR`
- [ ] `task_cwd_source == "explicit-task-dir"`
- [ ] resolved `reference_files` entry points at `$OVERRIDE_DIR/override.md`

## --task-dir requires existing directory

```bash
uv run meridian spawn -a reviewer -p "bad task dir" --task-dir "$SCRATCH/does-not-exist" --dry-run --json
```
- [ ] Fails with `task_dir does not exist` guidance

```bash
uv run meridian spawn -a reviewer -p "bad old prefix" -f @domain/page.md --dry-run --json
```
- [ ] Fails with message directing `@...` to `kb:...`

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

(
  cd "$WORKTREE"
  MERIDIAN_PROJECT_DIR=$CANONICAL MERIDIAN_HOME=$(mktemp -d) \
    uv run meridian spawn -a reviewer -p "test" --dry-run --json
)
```
- [ ] Exit 0
- [ ] `resolved_authority.project_root` matches `$CANONICAL` (not the worktree cwd)
- [ ] `resolved_authority.project_root_source == "explicit"`
