# Spawn dry-run routing provenance

Check the actual Mars bundle → Meridian JSON/text boundary in a packaged
workspace. No model request is made, but catalog refresh and native capability
probes may use the network. Use an installed Claude harness and an available
model alias (`sonnet` by default). Native eligibility may require authentication:
set `ROUTING_AUTH_FILE` to your Claude .credentials.json before setup to deliberately
copy only that file into the disposable store. API-key environment variables are
also retained; do not copy an entire native config/agent tree.

The [selection-report tests](../../integration/launch/test_selection_report.py)
protect protocol validation using a fake Mars executable. This guide keeps the
real integration check; the config/default precedence matrix belongs to
[spawn preparation](../../integration/ops/test_spawn_prepare_fork.py).

## Setup

Run from the checkout in a fresh shell.

```bash
export ROUTING_TOKEN="${ROUTING_TOKEN:-sonnet}"
. tests/smoke/scripts/setup.sh --git
trap smoke_cleanup EXIT
export ROUTING_OUTPUT="$SMOKE_ROOT/routing"
if [[ -n "${ROUTING_AUTH_FILE:-}" ]]; then
  install -m 600 "$ROUTING_AUTH_FILE" "$CLAUDE_CONFIG_DIR/.credentials.json"
fi
mkdir -p "$ROUTING_OUTPUT" "$SCRATCH/.mars/agents"
printf '[settings]\ntargets = [".claude"]\n' > "$SCRATCH/mars.toml"
printf '%s\n' '---' 'name: reviewer' 'description: routing smoke reviewer' \
  "model: $ROUTING_TOKEN" '---' '# Reviewer' > "$SCRATCH/.mars/agents/reviewer.md"
# An empty cache cannot prove alias canonicalization. This is a network probe.
uv run meridian mars models refresh
```

## JSON: requested alias, canonical model and routing source

```bash
uv run meridian --json spawn -a reviewer -p "Probe routing provenance." --dry-run \
  > "$ROUTING_OUTPUT/provenance.json"
uv run python - <<'PY'
import json
import os
from pathlib import Path
payload = json.loads((Path(os.environ['ROUTING_OUTPUT']) / 'provenance.json').read_text())
selection = payload['model_selection']
assert payload['status'] == 'dry-run' and payload['harness_id'] == 'claude'
assert selection['requested_token'] == os.environ['ROUTING_TOKEN']
assert selection['canonical_model_id'] == payload['model']
assert selection['canonical_model_id'] != os.environ['ROUTING_TOKEN']
report = payload['selection_report']
assert report['version'] == 3 and report['outcome'] == 'selected'
chosen = report['selected']
attempt = report['model_attempts'][chosen['attempt_index']]
assessment = attempt['assessments'][chosen['assessment_index']]
assert attempt['model_token'] == selection['requested_token']
assert attempt['canonical_model'] == selection['canonical_model_id']
assert assessment['harness'] == payload['harness_id']
assert attempt['model_source'] == 'profile'
assert selection['harness_provenance']
print('PASS: real Mars routing preserved alias, canonical model and selected route')
PY
```

## Text: same model and routing source

```bash
uv run meridian spawn -a reviewer -p "Probe routing provenance." --dry-run \
  > "$ROUTING_OUTPUT/provenance.txt"
uv run python - <<'PY'
import json
import os
from pathlib import Path
out = Path(os.environ['ROUTING_OUTPUT'])
payload = json.loads((out / 'provenance.json').read_text())
lines = (out / 'provenance.txt').read_text().splitlines()
assert 'Dry run complete.' in lines
assert f"Model: {payload['model']} (claude)" in lines
assert f"Routing: {payload['model_selection']['harness_provenance']}" in lines
print('PASS: text output agrees with the real bundle JSON')
PY
```

## Cleanup

```bash
smoke_cleanup
trap - EXIT
```
