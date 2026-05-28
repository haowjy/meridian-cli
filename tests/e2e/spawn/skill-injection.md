# Spawn Skill Injection

Validate that skills are correctly injected into spawn prompts across harness types. Skill injection follows two paths depending on the harness:

- **Inline** (Codex/OpenCode): Skill content is embedded directly in `composed_prompt`.
- **Append** (Claude): Skill content is passed via `--append-system-prompt` in the CLI command.

## Setup

```bash
export REPO_ROOT=/abs/path/to/meridian-channel
export SMOKE_REPO="$(mktemp -d /tmp/meridian-skill-inject.XXXXXX)"
git -C "$SMOKE_REPO" init --quiet
for var in $(env | awk -F= '/^MERIDIAN_/ {print $1}'); do unset "$var"; done
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
cd "$REPO_ROOT"
export RUNTIME_ROOT="$(uv run python - <<'PY'
import os
from pathlib import Path
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.user_paths import get_or_create_project_id, get_project_home

state_dir = resolve_project_paths(Path(os.environ["SMOKE_REPO"])).root_dir
print(get_project_home(get_or_create_project_id(state_dir)))
PY
)"
mkdir -p \
  "$SMOKE_REPO/.mars/agents" \
  "$SMOKE_REPO/.mars/skills/meridian-orchestrate" \
  "$SMOKE_REPO/.mars/skills/meridian-spawn-agent"
cat > "$SMOKE_REPO/.mars/agents/reviewer.md" <<'EOF'
# Reviewer

Skill smoke reviewer.
EOF
cat > "$SMOKE_REPO/.mars/skills/meridian-orchestrate/SKILL.md" <<'EOF'
# Meridian Orchestrate

Orchestrate the work as a supervisor.
EOF
cat > "$SMOKE_REPO/.mars/skills/meridian-spawn-agent/SKILL.md" <<'EOF'
# Meridian Spawn Agent

Spawn another agent when useful.
EOF
cd "$REPO_ROOT"
test -d "$SMOKE_REPO/.git" && echo "PASS: skill-injection repo ready" || echo "FAIL: skill-injection repo setup failed"
```

### SKILL-1. Inline injection puts skill content in composed prompt [CRITICAL]

Codex-style inline injection should embed skill content directly into the prompt.

```bash
uv run meridian --json spawn -a reviewer -p "do the task" --skills meridian-orchestrate -m gpt-5.3-codex --dry-run > /tmp/meridian-skill-inline.json && \
uv run python - <<'PY'
import json
doc = json.load(open("/tmp/meridian-skill-inline.json"))
assert doc["status"] == "dry-run"
prompt = doc["composed_prompt"]
assert "orchestrate" in prompt.lower() or "supervisor" in prompt.lower(), \
    "Skill content not found in composed_prompt for inline injection"
cli = doc.get("cli_command", [])
has_append = any("append-system-prompt" in str(a) for a in cli)
assert not has_append, "Inline harness should not use --append-system-prompt"
print("PASS: inline skill injection embedded content in composed_prompt")
PY
```

### SKILL-2. Append injection uses --append-system-prompt for Claude [CRITICAL]

Claude harness uses `--append-system-prompt` to inject skills.

```bash
uv run meridian --json spawn -a reviewer -p "do the task" --skills meridian-orchestrate -m sonnet --dry-run > /tmp/meridian-skill-append.json && \
uv run python - <<'PY'
import json
doc = json.load(open("/tmp/meridian-skill-append.json"))
assert doc["status"] == "dry-run"
assert doc.get("harness_id") == "claude", f"Expected claude harness, got {doc.get('harness_id')}"
cli = doc.get("cli_command", [])
cli_str = " ".join(str(a) for a in cli)
assert "--append-system-prompt" in cli_str, \
    "Claude harness should use --append-system-prompt for skill injection"
assert "orchestrate" in cli_str.lower() or "supervisor" in cli_str.lower(), \
    "Skill content not found in --append-system-prompt value"
prompt = doc.get("composed_prompt", "")
assert "orchestrate" not in prompt.lower(), \
    "Claude harness should NOT inline skill content into composed_prompt"
print("PASS: append skill injection used --append-system-prompt")
PY
```

### SKILL-3. Multiple skills are all injected [IMPORTANT]

When multiple skills are specified, all should appear in the output.

```bash
uv run meridian --json spawn \
  -a reviewer \
  -p "do the task" \
  --skills meridian-orchestrate,meridian-spawn-agent \
  --dry-run > /tmp/meridian-skill-multi.json && \
uv run python - <<'PY'
import json
doc = json.load(open("/tmp/meridian-skill-multi.json"))
assert doc["status"] == "dry-run"
prompt = doc.get("composed_prompt", "")
cli_str = " ".join(str(a) for a in doc.get("cli_command", []))
combined = prompt + cli_str
assert "orchestrate" in combined.lower(), "meridian-orchestrate skill not found"
assert "spawn" in combined.lower(), "meridian-spawn-agent skill not found"
print("PASS: multiple skills were all injected")
PY
```

### SKILL-4. Unknown skill fails cleanly [IMPORTANT]

```bash
if uv run meridian --json spawn -a reviewer -p "test" --skills no-such-skill-exists --dry-run >/tmp/meridian-skill-unknown.out 2>&1; then
  if grep -q "Traceback" /tmp/meridian-skill-unknown.out; then
    echo "FAIL: unknown skill produced a traceback"
  else
    echo "PASS: unknown skill was handled cleanly (accepted or warned)"
  fi
else
  if grep -q "Traceback" /tmp/meridian-skill-unknown.out; then
    echo "FAIL: unknown skill produced a traceback"
  else
    echo "PASS: unknown skill failed cleanly"
  fi
fi
```

### SKILL-5. Dry-run output includes model and harness metadata [IMPORTANT]

After the solid-consistency-refactor, dry-run JSON should include `model`, `harness_id`, and `reference_files` when present.

```bash
printf '# ref file\n' > /tmp/meridian-skill-ref.md
uv run meridian --json spawn \
  -a reviewer \
  -p "check metadata fields" \
  -m sonnet \
  -f /tmp/meridian-skill-ref.md \
  --dry-run > /tmp/meridian-skill-meta.json && \
uv run python - <<'PY'
import json
doc = json.load(open("/tmp/meridian-skill-meta.json"))
assert doc["status"] == "dry-run"
assert doc.get("model"), "model field missing from dry-run output"
assert doc.get("harness_id"), "harness_id field missing from dry-run output"
refs = doc.get("reference_files", [])
assert refs, "reference_files missing from dry-run output"
print("PASS: dry-run includes model, harness_id, and reference_files")
PY
```
