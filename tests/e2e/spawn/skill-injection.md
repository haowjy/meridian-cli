# Spawn Skill Injection

Validate that loaded skills reach spawn system prompts. Every harness gets loaded skills one way: as composition documents in the system prompt, after the agent profile. The user turn (`composed_prompt`) never carries them. Where the system prompt lands depends on the harness:

- **Codex**: developer instructions, visible in `cli_command`.
- **Claude**: the file named by `--append-system-prompt-file` (`system-prompt.md` in the spawn's log dir), written at launch.

## Setup

```bash
. tests/smoke/scripts/setup.sh --git
export REPO_ROOT="$SMOKE_ORIGINAL_CWD"
export SMOKE_REPO="$SCRATCH/skill-inject"
mkdir -p "$SMOKE_REPO"
git -C "$SMOKE_REPO" init --quiet
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
export MERIDIAN_TASK_DIR="$SMOKE_REPO"
export RUNTIME_ROOT="$(uv run python tests/e2e/resolve-runtime-root.py)"
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

### SKILL-1. Codex carries skill content in developer instructions [CRITICAL]

```bash
uv run meridian --json spawn -a reviewer -p "do the task" --skills meridian-orchestrate -m gpt-5.3-codex --dry-run > /tmp/meridian-skill-codex.json && \
uv run python - <<'PY'
import json
doc = json.load(open("/tmp/meridian-skill-codex.json"))
assert doc["status"] == "dry-run"
cli_str = " ".join(str(a) for a in doc.get("cli_command", []))
assert cli_str.count("# Skill: /meridian-orchestrate") == 1, \
    "Codex developer instructions should carry the skill exactly once"
assert "# Skill:" not in doc.get("composed_prompt", ""), \
    "Skill content should not be in the user turn"
print("PASS: codex skill in developer instructions, once")
PY
```

### SKILL-2. Claude reads skill content from its system prompt file [CRITICAL]

Dry run shows the flag; a real launch writes the file. Use a cheap model.

```bash
uv run meridian --json spawn -a reviewer -p "do the task" --skills meridian-orchestrate -m sonnet --dry-run > /tmp/meridian-skill-claude.json && \
uv run python - <<'PY'
import json
doc = json.load(open("/tmp/meridian-skill-claude.json"))
assert doc.get("harness_id") == "claude", f"Expected claude harness, got {doc.get('harness_id')}"
cli = [str(a) for a in doc.get("cli_command", [])]
assert "--append-system-prompt-file" in cli, "Claude should read its system prompt from a file"
assert "# Skill:" not in doc.get("composed_prompt", ""), "Skill content should not be in the user turn"
print("PASS: claude reads system prompt from file")
PY
SPAWN_ID=$(uv run meridian --json spawn -a reviewer -p "Reply OK." --skills meridian-orchestrate -m claude-haiku-4-5 | uv run python -c 'import json,sys; print(json.load(sys.stdin)["spawn_id"])')
COUNT=$(grep -c '^# Skill: /meridian-orchestrate' "$RUNTIME_ROOT/spawns/$SPAWN_ID/system-prompt.md")
test "$COUNT" = 1 && echo "PASS: system-prompt.md holds the skill once" || echo "FAIL: system-prompt.md skill count $COUNT"
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
