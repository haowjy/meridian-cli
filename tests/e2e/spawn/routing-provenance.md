# Spawn Dry-Run Routing Provenance

Validate the Phase 12 dry-run routing provenance surface in a realistic packaged workspace. This covers the user-visible gap between basic dry-run metadata and the new provenance output: preserve the requested model token, surface the resolved canonical model ID, and show the winning routing source in text mode.

## Setup

```bash
export REPO_ROOT=/abs/path/to/meridian-cli
export SMOKE_REPO="$(mktemp -d /tmp/meridian-routing.XXXXXX)"
git -C "$SMOKE_REPO" init --quiet
for var in $(env | awk -F= '/^MERIDIAN_/ {print $1}'); do unset "$var"; done
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
cd "$REPO_ROOT"
export RUNTIME_ROOT="$(uv run python tests/e2e/resolve-runtime-root.py)"

cat > "$SMOKE_REPO/mars.toml" <<'TOML'
[settings]
targets = [".claude"]
TOML

mkdir -p "$SMOKE_REPO/.mars/agents"
cat > "$SMOKE_REPO/.mars/agents/reviewer.md" <<'EOF_AGENT'
---
name: reviewer
description: routing provenance smoke reviewer
model: sonnet
---
# Reviewer

Reply briefly.
EOF_AGENT

cd "$REPO_ROOT"
echo "PASS: routing provenance workspace ready"
```

### ROUTE-1. JSON dry-run preserves requested token and canonical model [CRITICAL]

Request `sonnet` through the packaged agent profile. Dry-run JSON should preserve the requested token for provenance while also surfacing the resolved canonical model and routing source.

```bash
uv run meridian --json spawn -a reviewer -p "probe routing provenance" --dry-run \
  > /tmp/meridian-routing-provenance.json && \
uv run python - <<'PY'
import json

payload = json.load(open('/tmp/meridian-routing-provenance.json'))
selection = payload.get('model_selection') or {}

assert payload['status'] == 'dry-run'
assert payload['harness_id'] == 'claude', payload['harness_id']
assert selection['requested_token'] == 'sonnet', selection
assert selection['canonical_model_id'] == payload['model'], selection
assert selection['canonical_model_id'] != 'sonnet', selection
assert selection['harness_provenance'] == 'mars-provided', selection
print('PASS: ROUTE-1 dry-run JSON exposed requested token, canonical model, and routing provenance')
PY
```

### ROUTE-2. Text dry-run shows routing provenance summary [CRITICAL]

Text-mode dry-run should show the same resolved model and a routing summary line so a human can understand why that harness won without opening JSON.

```bash
EXPECTED_MODEL="$(uv run python - <<'PY'
import json
print(json.load(open('/tmp/meridian-routing-provenance.json'))['model'])
PY
)" && \
uv run meridian spawn -a reviewer -p "probe routing provenance" --dry-run \
  > /tmp/meridian-routing-provenance.txt && \
grep -q '^Dry run complete\.$' /tmp/meridian-routing-provenance.txt && \
grep -q "^Model: ${EXPECTED_MODEL} (claude)$" /tmp/meridian-routing-provenance.txt && \
grep -q '^Routing: mars-provided$' /tmp/meridian-routing-provenance.txt && \
echo "PASS: ROUTE-2 text dry-run surfaced routing provenance"
```

### ROUTE-3. Mars settings defaults own spawn routing when Meridian defaults are present [CRITICAL]

Project routing defaults belong in `mars.toml [settings]`, not `meridian.toml`.
This probe intentionally writes stale Meridian default keys and primary defaults,
then verifies a spawn with no explicit model/harness follows the Mars bundle
route instead of forwarding Meridian config values.

```bash
cat > "$SMOKE_REPO/mars.toml" <<'TOML'
[settings]
targets = [".claude", ".codex", ".opencode"]
default_model = "local-default-model"
default_harness = "opencode"
TOML

cat > "$SMOKE_REPO/.mars/agents/reviewer.md" <<'EOF_AGENT'
---
name: reviewer
description: project default routing smoke reviewer
---
# Reviewer

Reply briefly.
EOF_AGENT

cat > "$SMOKE_REPO/meridian.toml" <<'TOML'
[defaults]
model = "legacy-default-should-not-win"
harness = "claude"

[primary]
model = "primary-default-should-not-win"
harness = "opencode"
TOML

uv run meridian --json spawn -a reviewer -p "probe mars project defaults" --dry-run \
  > /tmp/meridian-routing-project-defaults.json && \
uv run python - <<'PY'
import json

payload = json.load(open('/tmp/meridian-routing-project-defaults.json'))
selection = payload.get('model_selection') or {}

assert payload['status'] == 'dry-run'
assert payload['model'] == 'local-default-model', payload
assert payload['harness_id'] == 'opencode', payload
assert selection['requested_token'] == 'local-default-model', selection
assert selection['canonical_model_id'] == 'local-default-model', selection
assert selection['harness_provenance'] == 'provider', selection
print('PASS: ROUTE-3 mars settings defaults drove spawn routing; Meridian defaults were ignored')
PY
```

## Cleanup

```bash
rm -rf "$SMOKE_REPO" \
  /tmp/meridian-routing-provenance.json \
  /tmp/meridian-routing-provenance.txt \
  /tmp/meridian-routing-project-defaults.json
unset MERIDIAN_PROJECT_DIR RUNTIME_ROOT SMOKE_REPO REPO_ROOT
echo "PASS: cleanup complete"
```
