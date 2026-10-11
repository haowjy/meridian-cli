# Fork

**Opt-in live tier:** these checks launch paid harness sessions. Choose cheap
models available to your account. Run from the checkout in a fresh shell;
explicitly supply credentials to the isolated native stores if needed. For OAuth,
set `CODEX_AUTH_FILE`, `CLAUDE_AUTH_FILE` or `OPENCODE_AUTH_FILE` before setup;
only the named auth file is copied, never the native agent/config tree.
Run cleanup even after a failed check. This is not automatic smoke.

Keep live fork/session boundaries here. Flag conflicts, environment aliases and
policy permutations belong to [argv normalization](../unit/cli/test_argv_normalization.py)
and [continue policy](../integration/ops/test_spawn_continue.py) regression tests.

## Setup: one completed source session

Set `FORK_MODEL` to an eligible cheap model and `FORK_HARNESS` to its harness
(default: `codex`) before sourcing setup. No model is chosen implicitly.

```bash
: "${FORK_MODEL:?Set FORK_MODEL to an eligible cheap model}"
export FORK_MODEL FORK_HARNESS="${FORK_HARNESS:-codex}"
. tests/smoke/scripts/setup.sh --git
export FORK_OUTPUT="$SMOKE_ROOT/fork"
mkdir -p "$FORK_OUTPUT"
printf '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n' > "$SCRATCH/mars.toml"
if [[ -n "${CODEX_AUTH_FILE:-}" ]]; then
  install -m 600 "$CODEX_AUTH_FILE" "$CODEX_HOME/auth.json"
fi
if [[ -n "${CLAUDE_AUTH_FILE:-}" ]]; then
  install -m 600 "$CLAUDE_AUTH_FILE" "$CLAUDE_CONFIG_DIR/.credentials.json"
fi
if [[ -n "${OPENCODE_AUTH_FILE:-}" ]]; then
  mkdir -p "$XDG_DATA_HOME/opencode"
  install -m 600 "$OPENCODE_AUTH_FILE" "$XDG_DATA_HOME/opencode/auth.json"
fi
smoke_add_agent reviewer
smoke_add_agent architect
uv run meridian --json --harness "$FORK_HARNESS" spawn -a reviewer \
  -m "$FORK_MODEL" -p "Reply briefly: fork smoke source." > "$FORK_OUTPUT/source.json"
export SOURCE_SPAWN_ID="$(uv run python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["FORK_OUTPUT"])/"source.json").read_text())["spawn_id"])')"
uv run meridian spawn wait "$SOURCE_SPAWN_ID"
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

root = resolve_runtime_paths(Path(os.environ['MERIDIAN_PROJECT_DIR'])).root_dir
row = spawn_store.get_spawn(root, os.environ['SOURCE_SPAWN_ID'])
assert row is not None and row.chat_id and row.harness_session_id
fields = ('chat_id', 'harness', 'harness_session_id', 'model', 'agent', 'work_id', 'prompt')
(Path(os.environ['FORK_OUTPUT']) / 'source-meta.json').write_text(
    json.dumps({field: getattr(row, field) for field in fields})
)
print('PASS: source completed and native identity recorded')
PY
export SOURCE_CHAT_ID="$(uv run python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["FORK_OUTPUT"])/"source-meta.json").read_text())["chat_id"])')"
export SOURCE_HARNESS_ID="$(uv run python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["FORK_OUTPUT"])/"source-meta.json").read_text())["harness_session_id"])')"
```

## Live forks from spawn and session references

Both references must create a distinct spawn, chat and native session. Wait for
each fork before inspecting persisted identity.

```bash
for REF in "$SOURCE_SPAWN_ID" "$SOURCE_CHAT_ID"; do
  uv run meridian --json spawn --fork "$REF" -p "Reply briefly: forked branch." \
    > "$FORK_OUTPUT/$REF.json"
  FORK_ID="$(uv run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["spawn_id"])' "$FORK_OUTPUT/$REF.json")"
  uv run meridian spawn wait "$FORK_ID"
done
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

out = Path(os.environ['FORK_OUTPUT'])
meta = json.loads((out / 'source-meta.json').read_text())
root = resolve_runtime_paths(Path(os.environ['MERIDIAN_PROJECT_DIR'])).root_dir
for ref in (os.environ['SOURCE_SPAWN_ID'], os.environ['SOURCE_CHAT_ID']):
    doc = json.loads((out / f'{ref}.json').read_text())
    assert doc['spawn_id'] != os.environ['SOURCE_SPAWN_ID']
    assert doc['forked_from'] == meta['chat_id']
    row = spawn_store.get_spawn(root, doc['spawn_id'])
    assert row is not None and row.chat_id != meta['chat_id']
    assert row.harness == meta['harness']
    assert row.harness_session_id and row.harness_session_id != meta['harness_session_id']
print('PASS: spawn/session references produced distinct native forks')
PY
```

## Root preview and fork-specific guidance

```bash
uv run meridian -C "$SCRATCH" --json --fork "$SOURCE_CHAT_ID" --dry-run > "$FORK_OUTPUT/root-preview.json"
uv run meridian --json spawn --fork "$SOURCE_SPAWN_ID" \
  -p "Check fork guidance." --dry-run > "$FORK_OUTPUT/guidance.json"
uv run python - <<'PY'
import json
import os
from pathlib import Path
out = Path(os.environ['FORK_OUTPUT'])
root = json.loads((out / 'root-preview.json').read_text())
assert root['message'] == 'Fork dry-run.'
assert root['forked_from'] == os.environ['SOURCE_CHAT_ID']
assert root['command']
spawn = json.loads((out / 'guidance.json').read_text())
assert spawn['status'] == 'dry-run' and 'spawn_id' not in spawn
assert spawn['cli_command'] and spawn['forked_from'] == os.environ['SOURCE_CHAT_ID']
prompt = spawn['composed_prompt']
assert 'You are working in a forked Meridian session' in prompt
assert 'You are resuming an existing Meridian session' not in prompt
print('PASS: root/spawn previews retain fork identity and guidance without launching')
PY
```

## Representative identity policy check

The automated suite owns the full override/conflict matrix. Keep one CLI-visible
identity rejection and one `--fork-fresh` override preview.

```bash
if uv run meridian spawn --fork "$SOURCE_SPAWN_ID" --agent architect \
  -p "Identity override." --dry-run > "$FORK_OUTPUT/identity-error.txt" 2>&1; then
  echo 'FAIL: identity-preserving fork accepted an agent override'; false
fi
grep -q -- '--fork preserves launch identity. Use --fork-fresh' "$FORK_OUTPUT/identity-error.txt"
uv run meridian --json spawn --fork-fresh "$SOURCE_SPAWN_ID" --agent architect \
  -p "Fresh identity." --dry-run > "$FORK_OUTPUT/fresh.json"
uv run python - <<'PY'
import json
import os
from pathlib import Path
doc = json.loads((Path(os.environ['FORK_OUTPUT']) / 'fresh.json').read_text())
assert doc['status'] == 'dry-run' and doc['agent'] == 'architect'
print('PASS: fork locks identity; fork-fresh permits a new agent')
PY
```

## Live harness matrix

Set `FORK_CLAUDE_MODEL`, `FORK_CODEX_MODEL` and `FORK_OPENCODE_MODEL` to cheap,
eligible models. Each row uses the real harness, not a fake executable; it
creates and drains both the source and the fork.

```bash
: "${FORK_CLAUDE_MODEL:?Set a cheap Claude model}"
: "${FORK_CODEX_MODEL:?Set a cheap Codex model}"
: "${FORK_OPENCODE_MODEL:?Set a cheap OpenCode model}"
for HARNESS in claude codex opencode; do
  case "$HARNESS" in
    claude) MODEL="$FORK_CLAUDE_MODEL" ;;
    codex) MODEL="$FORK_CODEX_MODEL" ;;
    opencode) MODEL="$FORK_OPENCODE_MODEL" ;;
  esac
  uv run meridian --json --harness "$HARNESS" spawn -a reviewer -m "$MODEL" \
    -p "Reply briefly: matrix source." > "$FORK_OUTPUT/$HARNESS-source.json"
  SEED_ID="$(uv run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["spawn_id"])' "$FORK_OUTPUT/$HARNESS-source.json")"
  uv run meridian spawn wait "$SEED_ID"
  uv run meridian --json --harness "$HARNESS" spawn --fork "$SEED_ID" \
    -p "Reply briefly: matrix fork." > "$FORK_OUTPUT/$HARNESS-fork.json"
  FORK_ID="$(uv run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["spawn_id"])' "$FORK_OUTPUT/$HARNESS-fork.json")"
  uv run meridian spawn wait "$FORK_ID"
  HARNESS="$HARNESS" SEED_ID="$SEED_ID" FORK_ID="$FORK_ID" uv run python - <<'PY'
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
root = resolve_runtime_paths(Path(os.environ['MERIDIAN_PROJECT_DIR'])).root_dir
seed = spawn_store.get_spawn(root, os.environ['SEED_ID'])
fork = spawn_store.get_spawn(root, os.environ['FORK_ID'])
assert seed is not None and fork is not None
assert seed.harness == fork.harness == os.environ['HARNESS']
assert seed.harness_session_id and fork.harness_session_id
assert seed.harness_session_id != fork.harness_session_id
print(f"PASS: {os.environ['HARNESS']} captured a distinct native fork")
PY
done
```

## Raw native session reference

The source is already tracked by Meridian. Looking it up by its native ID must
preserve the same chat lineage, not falsely classify it as an untracked session.

```bash
uv run meridian --json --harness "$FORK_HARNESS" spawn --fork "$SOURCE_HARNESS_ID" \
  -p "Reply briefly: raw native fork." > "$FORK_OUTPUT/raw.json"
RAW_ID="$(uv run python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["FORK_OUTPUT"])/"raw.json").read_text())["spawn_id"])')"
uv run meridian spawn wait "$RAW_ID"
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
root = resolve_runtime_paths(Path(os.environ['MERIDIAN_PROJECT_DIR'])).root_dir
doc = json.loads((Path(os.environ['FORK_OUTPUT']) / 'raw.json').read_text())
assert doc['forked_from'] == os.environ['SOURCE_CHAT_ID']
row = spawn_store.get_spawn(root, doc['spawn_id'])
assert row is not None and row.chat_id
records = session_store.get_session_records(root, {row.chat_id})
assert records and records[0].forked_from_chat_id == os.environ['SOURCE_CHAT_ID']
assert row.harness_session_id and row.harness_session_id != os.environ['SOURCE_HARNESS_ID']
print('PASS: raw native reference preserved tracked lineage and forked native identity')
PY
```

## Source immutability

```bash
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
before = json.loads((Path(os.environ['FORK_OUTPUT']) / 'source-meta.json').read_text())
root = resolve_runtime_paths(Path(os.environ['MERIDIAN_PROJECT_DIR'])).root_dir
row = spawn_store.get_spawn(root, os.environ['SOURCE_SPAWN_ID'])
assert row is not None
assert {field: getattr(row, field) for field in before} == before
print('PASS: source spawn/session metadata remained unchanged')
PY
```

## Native boundary failures

```bash
TARGET_HARNESS=claude
[ "$FORK_HARNESS" != claude ] || TARGET_HARNESS=codex
if uv run meridian --harness "$TARGET_HARNESS" spawn --fork "$SOURCE_SPAWN_ID" \
  -p "Cross-harness fork." > "$FORK_OUTPUT/cross-error.txt" 2>&1; then
  echo 'FAIL: cross-harness fork succeeded'; false
fi
grep -q 'Cannot fork across harnesses' "$FORK_OUTPUT/cross-error.txt"
if uv run meridian spawn --fork p999999999 -p "Missing source." > "$FORK_OUTPUT/missing-error.txt" 2>&1; then
  echo 'FAIL: nonexistent source succeeded'; false
fi
! grep -q 'Traceback' "$FORK_OUTPUT/missing-error.txt"
grep -Eiq 'not found|cannot|unknown|missing|session' "$FORK_OUTPUT/missing-error.txt"
MISSING_ID="$(uv run python - <<'PY'
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
root = resolve_runtime_paths(Path(os.environ['MERIDIAN_PROJECT_DIR'])).root_dir
sid = spawn_store.start_spawn(root, chat_id='c900001', model=os.environ['FORK_MODEL'],
    agent='reviewer', harness=os.environ['FORK_HARNESS'], prompt='Unbound fixture',
    harness_session_id=None)
spawn_store.finalize_spawn(root, str(sid), status='failed', exit_code=1, origin='runner')
print(sid)
PY
)"
if uv run meridian spawn --fork "$MISSING_ID" -p "Unbound source." > "$FORK_OUTPUT/unbound-error.txt" 2>&1; then
  echo 'FAIL: unbound source succeeded'; false
fi
grep -q 'has no recorded session' "$FORK_OUTPUT/unbound-error.txt"
echo 'PASS: native boundary failures were explicit'
```

## Cleanup

Every successful live command above waits for completion. If interrupted, first
cancel and drain the spawn IDs recorded in the output JSON files; do not remove
state while a runner is still active.

```bash
uv run meridian spawn list --view active
# If this lists active fixture spawns, cancel each ID and wait for it:
# uv run meridian spawn cancel <id>
# uv run meridian spawn wait <id>
# Only after the active list is empty:
smoke_cleanup
```
