# Spawn Lifecycle

Validate the normal v2 background flow: create a spawn, wait for completion, inspect `state.json`, verify report persistence, and confirm a few runtime query invariants. This file requires a working spawn harness in the current session because it exercises real state changes.

## Setup

```bash
export REPO_ROOT=/abs/path/to/meridian-channel
export SMOKE_REPO="$(mktemp -d /tmp/meridian-lifecycle.XXXXXX)"
git -C "$SMOKE_REPO" init --quiet
for var in $(env | awk -F= '/^MERIDIAN_/ {print $1}'); do unset "$var"; done
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
cd "$REPO_ROOT"
export RUNTIME_ROOT="$(uv run python tests/e2e/resolve-runtime-root.py)"
mkdir -p "$RUNTIME_ROOT/spawns" "$SMOKE_REPO/.mars/agents"
cat > "$SMOKE_REPO/.mars/agents/reviewer.md" <<'AGENT'
# Reviewer

You are a tiny smoke-test reviewer. Reply with one short sentence.
AGENT
cd "$REPO_ROOT"
uv run meridian spawn -h >/dev/null 2>&1 && echo "PASS: lifecycle setup complete" || echo "FAIL: lifecycle setup failed"
```

### LIFE-1. Background spawn returns an id [CRITICAL]

```bash
uv run meridian --json spawn -a reviewer -p "Say hello from smoke test" --bg > /tmp/meridian-lifecycle-create.json && \
uv run python - <<'PY'
import json
with open('/tmp/meridian-lifecycle-create.json', encoding='utf-8') as fh:
    doc = json.load(fh)
spawn_id = doc.get('spawn_id') or doc.get('id')
assert spawn_id, doc
print(f"PASS: created spawn {spawn_id}")
PY
```

### LIFE-2. Wait reaches a terminal status [CRITICAL]

```bash
SPAWN_ID="$(uv run python - <<'PY'
import json
print((json.load(open('/tmp/meridian-lifecycle-create.json'))['spawn_id']))
PY
)" && \
uv run meridian spawn wait "$SPAWN_ID" > /tmp/meridian-lifecycle-wait.txt 2>&1 || true

grep -Eq 'succeeded|failed|cancelled' /tmp/meridian-lifecycle-wait.txt && echo "PASS: wait reached a terminal status" || echo "FAIL: wait did not report a terminal status"
```

### LIFE-3. `spawn show --json` returns terminal metadata [IMPORTANT]

```bash
SPAWN_ID="$(uv run python - <<'PY'
import json
print((json.load(open('/tmp/meridian-lifecycle-create.json'))['spawn_id']))
PY
)" && \
uv run meridian --json spawn show "$SPAWN_ID" > /tmp/meridian-lifecycle-show.json && \
uv run python - <<'PY'
import json
with open('/tmp/meridian-lifecycle-show.json', encoding='utf-8') as fh:
    doc = json.load(fh)
assert doc['spawn_id']
assert doc['status'] in {'succeeded', 'failed', 'cancelled'}
assert 'report_path' in doc
print('PASS: spawn show returned terminal metadata')
PY
```

### LIFE-4. `state.json` exists, is v3, and omits the prompt body [CRITICAL]

```bash
SPAWN_ID="$(uv run python - <<'PY'
import json
print((json.load(open('/tmp/meridian-lifecycle-create.json'))['spawn_id']))
PY
)" && \
uv run python - <<'PY'
import json, os
from pathlib import Path
spawn_id = open('/tmp/meridian-lifecycle-create.json', encoding='utf-8').read()
doc = json.loads(spawn_id)
spawn_id = doc['spawn_id']
root = Path(os.environ['RUNTIME_ROOT'])
state_path = root / 'spawns' / spawn_id / 'state.json'
prompt_path = root / 'spawns' / spawn_id / 'starting-prompt.md'
state = json.loads(state_path.read_text(encoding='utf-8'))
prompt_text = prompt_path.read_text(encoding='utf-8')
assert state['v'] == 3
assert state['id'] == spawn_id
assert state['prompt_length'] == len(prompt_text)
assert prompt_text not in json.dumps(state)
print('PASS: state.json is v3 and prompt body stays in starting-prompt.md')
PY
```

### LIFE-5. Auto-extracted report is persisted and retrievable [IMPORTANT]

```bash
SPAWN_ID="$(uv run python - <<'PY'
import json
print((json.load(open('/tmp/meridian-lifecycle-create.json'))['spawn_id']))
PY
)" && \
uv run meridian spawn report show "$SPAWN_ID" > /tmp/meridian-lifecycle-report-show.txt && \
test -s "$RUNTIME_ROOT/spawns/$SPAWN_ID/report.md" && \
test -s /tmp/meridian-lifecycle-report-show.txt && \
echo "PASS: report.md exists and report show returned content" || echo "FAIL: report persistence or report show failed"
```

### LIFE-6. Stats reflect recorded runs [IMPORTANT]

```bash
uv run meridian spawn stats > /tmp/meridian-lifecycle-stats.txt && \
grep -q 'total_runs:' /tmp/meridian-lifecycle-stats.txt && \
grep -Eq 'succeeded:|failed:|cancelled:' /tmp/meridian-lifecycle-stats.txt && \
echo "PASS: spawn stats returned aggregate counts" || echo "FAIL: spawn stats output was incomplete"
```

### LIFE-7. Nested read (`_MERIDIAN_DEPTH>0`) does not stamp `orphan_run` [CRITICAL]

```bash
uv run python - <<'PY'
import json, os, subprocess
from pathlib import Path
from meridian.lib.state.spawn_store import start_spawn

root = Path(os.environ['RUNTIME_ROOT'])
spawn_id = str(
    start_spawn(
        root,
        spawn_id='p-depth-gate-smoke',
        chat_id='c-depth',
        model='gpt-5.4',
        agent='smoke',
        harness='codex',
        prompt='depth gate smoke',
        status='running',
        runner_pid=999999,
        started_at='2000-01-01T00:00:00Z',
    )
)
env = dict(os.environ)
env['_MERIDIAN_DEPTH'] = '1'
subprocess.run(
    ['uv', 'run', 'meridian', '--json', 'spawn', 'show', spawn_id],
    check=False,
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
state = json.loads((root / 'spawns' / spawn_id / 'state.json').read_text(encoding='utf-8'))
assert state['status'] == 'running'
assert state['error'] is None
print('PASS: depth>0 read left the row running without orphan reconciliation')
PY
```

### LIFE-8. `spawn list --status finalizing` returns v2 finalizing rows [IMPORTANT]

```bash
uv run python - <<'PY'
import os
from pathlib import Path
from meridian.lib.state.spawn_store import start_spawn

root = Path(os.environ['RUNTIME_ROOT'])
start_spawn(
    root,
    spawn_id='p-finalizing-filter-smoke',
    chat_id='c-finalizing',
    model='gpt-5.4',
    agent='smoke',
    harness='codex',
    prompt='finalizing filter smoke',
    status='finalizing',
    started_at='2026-04-12T15:00:00Z',
)
print('seeded')
PY

uv run meridian --json spawn list --status finalizing --limit 20 > /tmp/meridian-lifecycle-finalizing-list.json && \
uv run python - <<'PY'
import json
with open('/tmp/meridian-lifecycle-finalizing-list.json', encoding='utf-8') as fh:
    doc = json.load(fh)
spawns = doc.get('spawns', [])
assert any(row.get('spawn_id') == 'p-finalizing-filter-smoke' for row in spawns), spawns
assert all(row.get('status') == 'finalizing' for row in spawns), spawns
print('PASS: --status finalizing returned only finalizing rows')
PY
```

### LIFE-9. Late metadata updates do not downgrade a terminal row [IMPORTANT]

```bash
uv run python - <<'PY'
import os
from pathlib import Path

from meridian.lib.state.spawn_store import (
    finalize_spawn,
    get_spawn,
    mark_finalizing,
    start_spawn,
    update_spawn,
)

runtime_root = Path(os.environ['RUNTIME_ROOT'])
spawn_id = str(
    start_spawn(
        runtime_root,
        chat_id='c-lifecycle',
        model='gpt-5.4',
        agent='smoke',
        harness='codex',
        prompt='late update invariant smoke',
    )
)
finalize_spawn(runtime_root, spawn_id, status='succeeded', exit_code=0, origin='runner')
mark_finalizing(runtime_root, spawn_id)
update_spawn(runtime_root, spawn_id, desc='late update')

row = get_spawn(runtime_root, spawn_id)
assert row is not None
assert row.status == 'succeeded', f'terminal status downgraded: {row.status}'
assert row.desc == 'late update'
print('PASS: late metadata update did not downgrade terminal state')
PY
```
