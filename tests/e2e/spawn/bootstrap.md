# `meridian bootstrap`

Check two-tier bootstrap document ordering and an empty-doc workspace through the
packaged CLI. Dry-run makes no model request, but may probe installed harnesses
or refresh catalogs. These document-ordering checks are not replaced by runtime
state initialization tests.

## Setup

Run from the checkout in a fresh shell. Set `BOOTSTRAP_MODEL` to an available
Codex model; all state and outputs stay under owned scratch. If eligibility
requires login, set `CODEX_AUTH_FILE` to your auth.json to deliberately copy only
that file into the disposable store.

```bash
: "${BOOTSTRAP_MODEL:?Set BOOTSTRAP_MODEL to an available Codex model}"
export BOOTSTRAP_MODEL
. tests/smoke/scripts/setup.sh --git
trap smoke_cleanup EXIT
export REPO_ROOT="$SMOKE_ORIGINAL_CWD"
export BOOT_OUTPUT="$SMOKE_ROOT/bootstrap"
if [[ -n "${CODEX_AUTH_FILE:-}" ]]; then
  install -m 600 "$CODEX_AUTH_FILE" "$CODEX_HOME/auth.json"
fi
mkdir -p "$BOOT_OUTPUT" \
  "$SCRATCH/.mars/agents" \
  "$SCRATCH/.mars/skills/alpha/resources" \
  "$SCRATCH/.mars/skills/shared/resources" \
  "$SCRATCH/.mars/bootstrap/beta" \
  "$SCRATCH/.mars/bootstrap/shared"
printf '[settings]\ntargets = [".codex"]\n' > "$SCRATCH/mars.toml"
printf '# Bootstrap Smoke Agent\nReply briefly.\n' > "$SCRATCH/.mars/agents/bootstrap-smoke-agent.md"
printf 'alpha skill docs\n' > "$SCRATCH/.mars/skills/alpha/resources/BOOTSTRAP.md"
printf 'shared skill docs\n' > "$SCRATCH/.mars/skills/shared/resources/BOOTSTRAP.md"
printf 'beta package docs\n' > "$SCRATCH/.mars/bootstrap/beta/BOOTSTRAP.md"
printf 'shared package docs\n' > "$SCRATCH/.mars/bootstrap/shared/BOOTSTRAP.md"
```

## Two-tier ordering and explicit launch choices

```bash
(
  cd "$SCRATCH"
  uv run --project "$REPO_ROOT" meridian --json bootstrap --agent bootstrap-smoke-agent \
    --model "$BOOTSTRAP_MODEL" --harness codex --dry-run > "$BOOT_OUTPUT/two-tier.json"
)
uv run python - <<'PY'
import json
import os
from pathlib import Path
payload = json.loads((Path(os.environ['BOOT_OUTPUT']) / 'two-tier.json').read_text())
cmd = payload['command']
prompt = cmd[-1]
assert cmd[0] == 'codex' and '--model' in cmd
assert cmd[cmd.index('--model') + 1] == os.environ['BOOTSTRAP_MODEL']
assert prompt.startswith('# Bootstrap Smoke Agent')
expected = ['# Bootstrap: alpha', '# Bootstrap: shared',
    '# Bootstrap: beta (package)', '# Bootstrap: shared (package)']
positions = [prompt.index(marker) for marker in expected]
assert positions == sorted(positions)
for content in ('alpha skill docs', 'shared skill docs', 'beta package docs', 'shared package docs'):
    assert content in prompt
assert prompt.index('# Bootstrap: shared (package)') < prompt.index('# Meridian Context')
print('PASS: skill tier precedes package tier; explicit launch choices are visible')
PY
```

## No-doc workspace still launches normally

Rebind both authority and task roots; changing shell CWD alone would continue
using the first fixture's bootstrap documents.

```bash
EMPTY_REPO="$SMOKE_ROOT/bootstrap-empty"
mkdir -p "$EMPTY_REPO/.mars/agents"
cp "$SCRATCH/mars.toml" "$EMPTY_REPO/"
cp "$SCRATCH/.mars/agents/bootstrap-smoke-agent.md" "$EMPTY_REPO/.mars/agents/"
(
  cd "$EMPTY_REPO"
  MERIDIAN_PROJECT_DIR="$EMPTY_REPO" MERIDIAN_TASK_DIR="$EMPTY_REPO" \
    uv run --project "$REPO_ROOT" meridian --json bootstrap --agent bootstrap-smoke-agent \
      --model "$BOOTSTRAP_MODEL" --harness codex --dry-run > "$BOOT_OUTPUT/empty.json"
)
uv run python - <<'PY'
import json
import os
from pathlib import Path
cmd = json.loads((Path(os.environ['BOOT_OUTPUT']) / 'empty.json').read_text())['command']
assert cmd[0] == 'codex'
assert '# Bootstrap:' not in cmd[-1]
assert '# Bootstrap Smoke Agent' in cmd[-1]
print('PASS: empty-doc workspace produced a normal launch preview')
PY
```

## Cleanup

```bash
smoke_cleanup
trap - EXIT
```
