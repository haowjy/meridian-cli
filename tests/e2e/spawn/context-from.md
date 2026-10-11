# Spawn `--from` context references

Check packaged CLI prompt rendering for spawn and session references. These are
synthetic-state dry-runs, not live harness/session verification. Dry-run may probe
installed harnesses or refresh catalogs; it makes no model request. Use a Codex
model available in your installation via `CONTEXT_MODEL`. If native eligibility
requires login, set `CODEX_AUTH_FILE` to your auth.json before setup to deliberately
copy only that file into the disposable store; no agent/config tree is copied.

Flag conflicts are covered by [argv normalization](../../unit/cli/test_argv_normalization.py);
reference resolution is covered by [context-ref integration](../../integration/ops/test_context_ref.py).
Keep the rendered report/transcript boundary here rather than a second policy matrix.

## Setup

Run from the checkout in a fresh shell. All state and outputs stay in owned scratch.

```bash
: "${CONTEXT_MODEL:?Set CONTEXT_MODEL to an available Codex model}"
export CONTEXT_MODEL
. tests/smoke/scripts/setup.sh --git
trap smoke_cleanup EXIT
smoke_add_agent coder
printf '[settings]\ntargets = [".codex"]\n' > "$SCRATCH/mars.toml"
if [[ -n "${CODEX_AUTH_FILE:-}" ]]; then
  install -m 600 "$CODEX_AUTH_FILE" "$CODEX_HOME/auth.json"
fi
export FROM_OUTPUT="$SMOKE_ROOT/context-from"
mkdir -p "$FROM_OUTPUT"
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

root = resolve_runtime_paths(Path(os.environ['MERIDIAN_PROJECT_DIR'])).root_dir
chat = session_store.start_session(root, harness='codex', harness_session_id='',
    model=os.environ['CONTEXT_MODEL'], kind='primary')
ids = []
for kind, status in (('primary', 'succeeded'), ('child', 'succeeded'), ('child', 'failed')):
    sid = str(spawn_store.start_spawn(root, chat_id=chat, model=os.environ['CONTEXT_MODEL'],
        agent='coder', harness='codex', kind=kind, prompt='Context fixture'))
    spawn_store.finalize_spawn(root, sid, status=status,
        exit_code=0 if status == 'succeeded' else 1, origin='runner')
    ids.append(sid)
(root / 'spawns' / ids[1] / 'report.md').write_text('# Phase 1 Report\nImplemented data model.\n')
session_store.stop_session(root, chat)
(Path(os.environ['FROM_OUTPUT']) / 'seed.json').write_text(json.dumps({
    'chat': chat, 'primary': ids[0], 'report': ids[1], 'no_report': ids[2],
}))
PY
export SEED_ID="$(uv run python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["FROM_OUTPUT"])/"seed.json").read_text())["report"])')"
export NO_REPORT_ID="$(uv run python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["FROM_OUTPUT"])/"seed.json").read_text())["no_report"])')"
export CHAT_ID="$(uv run python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["FROM_OUTPUT"])/"seed.json").read_text())["chat"])')"
```

## Spawn references: multiple blocks, report and missing-report fallback

```bash
uv run meridian --json spawn -a coder --harness codex -m "$CONTEXT_MODEL" \
  --from "$SEED_ID" --from "$NO_REPORT_ID" --dry-run -p "Build on prior work." \
  > "$FROM_OUTPUT/spawns.json"
uv run python - <<'PY'
import json
import os
from pathlib import Path
out = Path(os.environ['FROM_OUTPUT'])
seed = json.loads((out / 'seed.json').read_text())
doc = json.loads((out / 'spawns.json').read_text())
assert doc['status'] == 'dry-run'
assert doc['context_from_resolved'] == [seed['report'], seed['no_report']]
prompt = doc['composed_prompt']
assert prompt.count('<prior-spawn-context') == 2
for sid in (seed['report'], seed['no_report']):
    assert f'<prior-spawn-context spawn="{sid}">' in prompt
    assert f'meridian spawn show {sid}' in prompt
assert '## Report' in prompt and 'Phase 1 Report' in prompt
assert 'No report available.' in prompt and '## Explore Further' in prompt
assert '## Files Modified' not in prompt
print('PASS: spawn references preserve both blocks and report availability')
PY
```

## Session reference: primary transcript, not a child report

```bash
uv run meridian --json spawn -a coder --harness codex -m "$CONTEXT_MODEL" \
  --from "$CHAT_ID" --dry-run -p "Review the prior session." > "$FROM_OUTPUT/session.json"
uv run python - <<'PY'
import json
import os
from pathlib import Path
out = Path(os.environ['FROM_OUTPUT'])
seed = json.loads((out / 'seed.json').read_text())
doc = json.loads((out / 'session.json').read_text())
assert doc['status'] == 'dry-run' and doc['context_from_resolved'] == [seed['chat']]
prompt = doc['composed_prompt']
assert f'<prior-session-context chat="{seed["chat"]}" primary_spawn="{seed["primary"]}">' in prompt
assert 'Phase 1 Report' not in prompt
assert f'meridian session log {seed["chat"]}' in prompt
assert f'meridian spawn show {seed["primary"]}' in prompt
print('PASS: session reference points to its primary transcript, not either child')
PY
```

## Cleanup

```bash
smoke_cleanup
trap - EXIT
```
