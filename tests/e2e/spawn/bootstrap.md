# `meridian bootstrap`

Validate the user-facing bootstrap flow with a disposable workspace. Use dry-run so the check stays lightweight while still proving which bootstrap docs land in the launched prompt.

## Setup

```bash
. tests/smoke/scripts/setup.sh --git
export REPO_ROOT="$SMOKE_ORIGINAL_CWD"
export SMOKE_REPO="$SCRATCH/bootstrap"
mkdir -p "$SMOKE_REPO"
git -C "$SMOKE_REPO" init --quiet
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
export MERIDIAN_TASK_DIR="$SMOKE_REPO"
export UV_CACHE_DIR="$SMOKE_ROOT/uv-cache"

cat > "$SMOKE_REPO/mars.toml" <<'EOF_MARS'
[settings]
targets = [".claude"]
EOF_MARS

mkdir -p \
  "$SMOKE_REPO/.mars/agents" \
  "$SMOKE_REPO/.mars/skills/alpha/resources" \
  "$SMOKE_REPO/.mars/skills/shared/resources" \
  "$SMOKE_REPO/.mars/bootstrap/beta" \
  "$SMOKE_REPO/.mars/bootstrap/shared"

cat > "$SMOKE_REPO/.mars/agents/bootstrap-smoke-agent.md" <<'EOF_AGENT'
---
name: bootstrap-smoke-agent
description: bootstrap smoke agent
model: gpt-5.3-codex
sandbox: workspace-write
---
# Bootstrap Smoke Agent

Reply briefly.
EOF_AGENT

printf 'alpha skill docs\n' > "$SMOKE_REPO/.mars/skills/alpha/resources/BOOTSTRAP.md"
printf 'shared skill docs\n' > "$SMOKE_REPO/.mars/skills/shared/resources/BOOTSTRAP.md"
printf 'beta package docs\n' > "$SMOKE_REPO/.mars/bootstrap/beta/BOOTSTRAP.md"
printf 'shared package docs\n' > "$SMOKE_REPO/.mars/bootstrap/shared/BOOTSTRAP.md"

echo "PASS: bootstrap smoke workspace ready"
```

Run the CLI from the disposable workspace while still using the local meridian source tree:

```bash
cd "$SMOKE_REPO"
```

### BOOT-1. Two-tier bootstrap docs are injected in deterministic order [CRITICAL]

```bash
uv run --project "$REPO_ROOT" meridian --json bootstrap \
  --agent bootstrap-smoke-agent \
  --model gpt-5.3-codex \
  --harness codex \
  --dry-run > /tmp/meridian-bootstrap-two-tier.json && \
uv run python - <<'PY'
import json

payload = json.load(open('/tmp/meridian-bootstrap-two-tier.json'))
cmd = payload['command']
prompt = cmd[-1]
expected = [
    '# Bootstrap: alpha',
    '# Bootstrap: shared',
    '# Bootstrap: beta (package)',
    '# Bootstrap: shared (package)',
]
positions = [prompt.index(marker) for marker in expected]
assert positions == sorted(positions), positions
assert 'alpha skill docs' in prompt
assert 'shared skill docs' in prompt
assert 'beta package docs' in prompt
assert 'shared package docs' in prompt
assert prompt.index('# Bootstrap: shared (package)') < prompt.index('# Meridian Context')
print('PASS: BOOT-1 injected skill-tier docs first, then package-tier docs, into the launched prompt')
PY
```

### BOOT-2. Bootstrap still launches when no docs exist [CRITICAL]

```bash
EMPTY_REPO="$SMOKE_ROOT/bootstrap-empty" && mkdir -p "$EMPTY_REPO" && \
cat > "$EMPTY_REPO/mars.toml" <<'EOF_MARS'
[settings]
targets = [".claude"]
EOF_MARS
mkdir -p "$EMPTY_REPO/.mars/agents" && \
cat > "$EMPTY_REPO/.mars/agents/bootstrap-smoke-agent.md" <<'EOF_AGENT'
---
name: bootstrap-smoke-agent
description: bootstrap smoke agent
model: gpt-5.3-codex
sandbox: workspace-write
---
# Bootstrap Smoke Agent

Reply briefly.
EOF_AGENT
cd "$EMPTY_REPO" && \
uv run --project "$REPO_ROOT" meridian --json bootstrap \
  --agent bootstrap-smoke-agent \
  --model gpt-5.3-codex \
  --harness codex \
  --dry-run > /tmp/meridian-bootstrap-empty.json && \
uv run python - <<'PY'
import json

payload = json.load(open('/tmp/meridian-bootstrap-empty.json'))
cmd = payload['command']
prompt = cmd[-1]
assert cmd[0] == 'codex', cmd
assert '# Bootstrap:' not in prompt, prompt
assert '# Bootstrap Smoke Agent' in prompt, prompt
print('PASS: BOOT-2 no-doc workspace still produced a normal bootstrap launch dry-run')
PY
```

### BOOT-3. Explicit flags stay user-visible in dry-run output [IMPORTANT]

This piggybacks on `BOOT-1` so the check stays cheap.

```bash
uv run python - <<'PY'
import json

payload = json.load(open('/tmp/meridian-bootstrap-two-tier.json'))
cmd = payload['command']
prompt = cmd[-1]
assert cmd[0] == 'codex', cmd
assert '--model' in cmd, cmd
assert 'gpt-5.3-codex' in cmd, cmd
assert prompt.startswith('# Bootstrap Smoke Agent'), prompt[:120]
print('PASS: BOOT-3 dry-run kept harness/model/agent choices visible to the user')
PY
```

## Cleanup

```bash
rm -rf "$SMOKE_REPO" "$EMPTY_REPO" \
  /tmp/meridian-bootstrap-two-tier.json \
  /tmp/meridian-bootstrap-empty.json
unset REPO_ROOT SMOKE_REPO EMPTY_REPO UV_CACHE_DIR

echo "PASS: cleanup complete"
```
