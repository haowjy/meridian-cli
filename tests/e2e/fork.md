# Fork

These checks validate fork flows on both root and spawn commands:
- `--fork [ref]` (identity-preserving)
- `--fork-fresh [ref]` (identity-changing)
- `--from [ref]` (reference-only context seeding)

This suite requires a working harness because several scenarios execute real forks.

## Setup

```bash
export REPO_ROOT=/abs/path/to/meridian-channel
export SMOKE_REPO="$(mktemp -d /tmp/meridian-fork.XXXXXX)"
git -C "$SMOKE_REPO" init --quiet
for var in $(env | awk -F= '/^MERIDIAN_/ {print $1}'); do unset "$var"; done
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
cd "$REPO_ROOT"
export RUNTIME_ROOT="$(uv run python tests/e2e/resolve-runtime-root.py)"
mkdir -p "$SMOKE_REPO/.mars/agents"
cat > "$SMOKE_REPO/.mars/agents/reviewer.md" <<'EOF'
# Reviewer

Fork smoke reviewer. Keep answers short.
EOF
cat > "$SMOKE_REPO/.mars/agents/architect.md" <<'EOF'
# Architect

Fork smoke architect. Keep answers short.
EOF
cd "$REPO_ROOT"
uv run meridian --json spawn -a reviewer -p "Seed session for fork smoke tests." > /tmp/meridian-fork-source-create.json && \
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
doc = json.loads(Path('/tmp/meridian-fork-source-create.json').read_text(encoding='utf-8'))
print(doc.get('spawn_id', ''))
PY
)" && \
[ -n "$SOURCE_SPAWN_ID" ] && \
uv run meridian spawn wait "$SOURCE_SPAWN_ID" >/tmp/meridian-fork-source-wait.txt 2>&1 || true
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

doc = json.loads(Path("/tmp/meridian-fork-source-create.json").read_text(encoding="utf-8"))
source_spawn_id = doc.get("spawn_id")
assert source_spawn_id
project_root = Path(os.environ["MERIDIAN_PROJECT_DIR"])
runtime_root = resolve_runtime_paths(project_root).root_dir
row = spawn_store.get_spawn(runtime_root, source_spawn_id)
assert row is not None
assert row.chat_id
assert row.harness_session_id
meta = {
    "source_spawn_id": row.id,
    "source_chat_id": row.chat_id,
    "source_harness_session_id": row.harness_session_id,
    "source_harness": row.harness,
    "source_model": row.model,
    "source_agent": row.agent,
    "source_work_id": row.work_id,
}
Path("/tmp/meridian-fork-source-meta.json").write_text(
    json.dumps(meta, sort_keys=True), encoding="utf-8"
)
row_before = {
    "chat_id": row.chat_id,
    "harness": row.harness,
    "harness_session_id": row.harness_session_id,
    "model": row.model,
    "agent": row.agent,
    "work_id": row.work_id,
    "prompt": row.prompt,
}
Path("/tmp/meridian-fork-source-row-before.json").write_text(
    json.dumps(row_before, sort_keys=True), encoding="utf-8"
)
print("PASS: fork smoke setup complete")
PY
```

### FORK-1. `spawn --fork <spawn_id> -p` creates a new spawn and chat [CRITICAL]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
uv run meridian --json spawn --fork "$SOURCE_SPAWN_ID" -p "Branch from source spawn." > /tmp/meridian-fork-1.json && \
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

meta = json.loads(Path("/tmp/meridian-fork-source-meta.json").read_text(encoding="utf-8"))
doc = json.loads(Path("/tmp/meridian-fork-1.json").read_text(encoding="utf-8"))
new_spawn_id = doc.get("spawn_id")
assert new_spawn_id and new_spawn_id != meta["source_spawn_id"]
assert doc.get("forked_from") == meta["source_chat_id"]
runtime_root = resolve_runtime_paths(Path(os.environ["MERIDIAN_PROJECT_DIR"])).root_dir
source_row = spawn_store.get_spawn(runtime_root, meta["source_spawn_id"])
new_row = spawn_store.get_spawn(runtime_root, new_spawn_id)
assert source_row is not None and new_row is not None
assert new_row.chat_id and source_row.chat_id and new_row.chat_id != source_row.chat_id
print("PASS: fork from spawn id created a distinct spawn/session")
PY
```

### FORK-2. `--fork <session_id>` root flow exposes fork output contract [CRITICAL]

```bash
SOURCE_CHAT_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_chat_id'])
PY
)" && \
uv run meridian --json --fork "$SOURCE_CHAT_ID" --dry-run > /tmp/meridian-fork-2.json && \
uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path("/tmp/meridian-fork-source-meta.json").read_text(encoding="utf-8"))
doc = json.loads(Path("/tmp/meridian-fork-2.json").read_text(encoding="utf-8"))
assert doc["message"] == "Fork dry-run."
assert doc["forked_from"] == meta["source_chat_id"]
assert isinstance(doc.get("command"), list) and doc["command"]
print("PASS: root --fork dry-run emits fork-specific output")
PY
```

### FORK-3. `spawn --fork <session_id> -p` works from session refs [CRITICAL]

```bash
SOURCE_CHAT_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_chat_id'])
PY
)" && \
uv run meridian --json spawn --fork "$SOURCE_CHAT_ID" -p "Branch from source session." > /tmp/meridian-fork-3.json && \
uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path("/tmp/meridian-fork-source-meta.json").read_text(encoding="utf-8"))
doc = json.loads(Path("/tmp/meridian-fork-3.json").read_text(encoding="utf-8"))
assert doc.get("spawn_id")
assert doc.get("forked_from") == meta["source_chat_id"]
print("PASS: spawn --fork accepts session ids")
PY
```

### FORK-3b. Bare `--fork` uses `$MERIDIAN_SPAWN_ID` [CRITICAL]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
SOURCE_CHAT_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_chat_id'])
PY
)" && \
MERIDIAN_SPAWN_ID="$SOURCE_SPAWN_ID" uv run meridian --json spawn --fork -p "Bare fork from env." --dry-run > /tmp/meridian-fork-3b-spawn.json && \
MERIDIAN_SPAWN_ID="$SOURCE_SPAWN_ID" uv run meridian --json --fork --dry-run > /tmp/meridian-fork-3b-root.json && \
SOURCE_CHAT_ID="$SOURCE_CHAT_ID" uv run python - <<'PY'
import json
import os
from pathlib import Path

spawn_doc = json.loads(Path("/tmp/meridian-fork-3b-spawn.json").read_text(encoding="utf-8"))
root_doc = json.loads(Path("/tmp/meridian-fork-3b-root.json").read_text(encoding="utf-8"))
assert spawn_doc["status"] == "dry-run"
assert spawn_doc.get("forked_from") == os.environ["SOURCE_CHAT_ID"]
assert root_doc["message"] == "Fork dry-run."
assert root_doc.get("forked_from") == os.environ["SOURCE_CHAT_ID"]
print("PASS: bare --fork resolved from MERIDIAN_SPAWN_ID for spawn and root")
PY
```

### FORK-3c. Bare `--from` uses `$MERIDIAN_SPAWN_ID` [NORMAL]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
MERIDIAN_SPAWN_ID="$SOURCE_SPAWN_ID" uv run meridian --json spawn --from -p "review what I just did" --dry-run > /tmp/meridian-fork-3c.json && \
SOURCE_SPAWN_ID="$SOURCE_SPAWN_ID" uv run python - <<'PY'
import json
import os
from pathlib import Path

doc = json.loads(Path("/tmp/meridian-fork-3c.json").read_text(encoding="utf-8"))
assert doc["status"] == "dry-run"
assert doc.get("context_from") == [os.environ["SOURCE_SPAWN_ID"]]
print("PASS: bare --from resolved from MERIDIAN_SPAWN_ID")
PY
```

### FORK-3d. `--from` and `--continue` are mutually exclusive [CRITICAL]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)"
if uv run meridian spawn --from "$SOURCE_SPAWN_ID" --continue "$SOURCE_SPAWN_ID" -p "should fail" >/tmp/meridian-fork-3d.out 2>&1; then
  echo "FAIL: --from + --continue unexpectedly succeeded"
elif grep -q "Cannot combine --from with --continue." /tmp/meridian-fork-3d.out; then
  echo "PASS: --from + --continue rejected cleanly"
else
  echo "FAIL: --from + --continue error text was not useful"
fi
```

### FORK-4. `--fork` and `--from` are mutually exclusive [CRITICAL]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)"
if uv run meridian spawn --fork "$SOURCE_SPAWN_ID" --from "$SOURCE_SPAWN_ID" -p "should fail" >/tmp/meridian-fork-4.out 2>&1; then
  echo "FAIL: --fork + --from unexpectedly succeeded"
elif grep -q "Cannot combine --fork with --from" /tmp/meridian-fork-4.out; then
  echo "PASS: --fork + --from rejected cleanly"
else
  echo "FAIL: --fork + --from error text was not useful"
fi
```

### FORK-5. Root `--fork` and `--continue` are mutually exclusive [CRITICAL]

```bash
SOURCE_CHAT_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_chat_id'])
PY
)"
if uv run meridian --fork "$SOURCE_CHAT_ID" --continue "$SOURCE_CHAT_ID" --dry-run >/tmp/meridian-fork-5.out 2>&1; then
  echo "FAIL: --fork + --continue unexpectedly succeeded"
elif grep -q "Cannot combine --fork with --continue" /tmp/meridian-fork-5.out; then
  echo "PASS: root --fork + --continue rejected cleanly"
else
  echo "FAIL: root conflict error text was not useful"
fi
```

### FORK-6. Model override on `--fork` is rejected [IMPORTANT]

```bash
MODEL_OVERRIDE="${MODEL_OVERRIDE:-gpt-5.4}"
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
if uv run meridian spawn --fork "$SOURCE_SPAWN_ID" -m "$MODEL_OVERRIDE" -p "Model override smoke." --dry-run >/tmp/meridian-fork-6.out 2>&1; then
  echo "FAIL: --fork + -m unexpectedly succeeded"
elif grep -q -- "--fork preserves launch identity. Use --fork-fresh to change agent, model, or skills." /tmp/meridian-fork-6.out; then
  echo "PASS: --fork + -m rejected with identity-lock guidance"
else
  echo "FAIL: --fork + -m error text was not useful"
fi
```

### FORK-7. Agent override on `--fork` is rejected [IMPORTANT]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
if uv run meridian spawn --fork "$SOURCE_SPAWN_ID" --agent architect -p "Agent override smoke." --dry-run >/tmp/meridian-fork-7.out 2>&1; then
  echo "FAIL: --fork + --agent unexpectedly succeeded"
elif grep -q -- "--fork preserves launch identity. Use --fork-fresh to change agent, model, or skills." /tmp/meridian-fork-7.out; then
  echo "PASS: --fork + --agent rejected with identity-lock guidance"
else
  echo "FAIL: --fork + --agent error text was not useful"
fi
```

### FORK-7b. `--fork-fresh` honors model override [IMPORTANT]

```bash
MODEL_OVERRIDE="${MODEL_OVERRIDE:-gpt-5.4}"
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
uv run meridian --json spawn --fork-fresh "$SOURCE_SPAWN_ID" -m "$MODEL_OVERRIDE" -p "Model override smoke." --dry-run > /tmp/meridian-fork-7b.json && \
MODEL_OVERRIDE="$MODEL_OVERRIDE" uv run python - <<'PY'
import json
import os
from pathlib import Path
doc = json.loads(Path("/tmp/meridian-fork-7b.json").read_text(encoding="utf-8"))
assert doc["status"] == "dry-run"
assert doc.get("model") == os.environ["MODEL_OVERRIDE"]
print("PASS: --fork-fresh dry-run honored model override")
PY
```

### FORK-7c. `--fork-fresh` honors agent override [IMPORTANT]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
uv run meridian --json spawn --fork-fresh "$SOURCE_SPAWN_ID" --agent architect -p "Agent override smoke." --dry-run > /tmp/meridian-fork-7c.json && \
uv run python - <<'PY'
import json
from pathlib import Path
doc = json.loads(Path("/tmp/meridian-fork-7c.json").read_text(encoding="utf-8"))
assert doc["status"] == "dry-run"
assert doc.get("agent") == "architect"
print("PASS: --fork-fresh dry-run honored agent override")
PY
```

### FORK-8. Fork works with `--yolo` [IMPORTANT]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
uv run meridian --json spawn --fork "$SOURCE_SPAWN_ID" --yolo -p "YOLO fork smoke." --dry-run > /tmp/meridian-fork-8.json && \
uv run python - <<'PY'
import json
from pathlib import Path
doc = json.loads(Path("/tmp/meridian-fork-8.json").read_text(encoding="utf-8"))
assert doc["status"] == "dry-run"
print("PASS: fork + --yolo was accepted")
PY
```

### FORK-9. Fork can target a different work item [IMPORTANT]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
ALT_WORK="fork-smoke-alt-work" && \
uv run meridian --json spawn --fork "$SOURCE_SPAWN_ID" --work "$ALT_WORK" -p "Fork into different work item." > /tmp/meridian-fork-9.json && \
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

doc = json.loads(Path("/tmp/meridian-fork-9.json").read_text(encoding="utf-8"))
spawn_id = doc.get("spawn_id")
assert spawn_id
runtime_root = resolve_runtime_paths(Path(os.environ["MERIDIAN_PROJECT_DIR"])).root_dir
row = spawn_store.get_spawn(runtime_root, spawn_id)
assert row is not None
assert row.work_id == "fork-smoke-alt-work"
print("PASS: fork spawn attached to override work item")
PY
```

### FORK-10. Harness matrix (claude/codex/opencode) [IMPORTANT]

```bash
FAIL=0
for HARNESS in claude codex opencode; do
  if ! uv run meridian --json --harness "$HARNESS" spawn -a reviewer -p "Seed $HARNESS fork smoke." > "/tmp/meridian-fork-10-${HARNESS}-seed.json"; then
    echo "FAIL: could not create $HARNESS seed spawn"
    FAIL=1
    continue
  fi

  SEED_SPAWN_ID="$(HARNESS="$HARNESS" uv run python - <<'PY'
import json
import os
from pathlib import Path
harness = os.environ["HARNESS"]
doc = json.loads(Path(f"/tmp/meridian-fork-10-{harness}-seed.json").read_text(encoding="utf-8"))
print(doc.get("spawn_id", ""))
PY
)" || FAIL=1
  [ -n "$SEED_SPAWN_ID" ] || FAIL=1
  uv run meridian spawn wait "$SEED_SPAWN_ID" >/tmp/meridian-fork-10-wait-"$HARNESS".txt 2>&1 || true

  if ! uv run meridian --json --harness "$HARNESS" spawn --fork "$SEED_SPAWN_ID" -p "Fork on $HARNESS." > "/tmp/meridian-fork-10-${HARNESS}-fork.json"; then
    echo "FAIL: could not fork on $HARNESS"
    FAIL=1
    continue
  fi

  if ! HARNESS="$HARNESS" SEED_SPAWN_ID="$SEED_SPAWN_ID" uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

harness = os.environ["HARNESS"]
seed_spawn_id = os.environ["SEED_SPAWN_ID"]
fork_doc = json.loads(Path(f"/tmp/meridian-fork-10-{harness}-fork.json").read_text(encoding="utf-8"))
fork_spawn_id = fork_doc.get("spawn_id")
assert fork_spawn_id
runtime_root = resolve_runtime_paths(Path(os.environ["MERIDIAN_PROJECT_DIR"])).root_dir
seed_row = spawn_store.get_spawn(runtime_root, seed_spawn_id)
fork_row = spawn_store.get_spawn(runtime_root, fork_spawn_id)
assert seed_row is not None and fork_row is not None
assert seed_row.harness == harness
assert fork_row.harness == harness
assert seed_row.harness_session_id
assert fork_row.harness_session_id
assert seed_row.harness_session_id != fork_row.harness_session_id
print(f"PASS: {harness} fork captured a distinct harness session")
PY
  then
    FAIL=1
  fi
done
[ "$FAIL" -eq 0 ] && echo "PASS: harness matrix passed" || echo "FAIL: harness matrix had failures"
```

### FORK-11. Source session remains untouched after fork [CRITICAL]

```bash
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

meta = json.loads(Path("/tmp/meridian-fork-source-meta.json").read_text(encoding="utf-8"))
before = json.loads(Path("/tmp/meridian-fork-source-row-before.json").read_text(encoding="utf-8"))
runtime_root = resolve_runtime_paths(Path(os.environ["MERIDIAN_PROJECT_DIR"])).root_dir
row = spawn_store.get_spawn(runtime_root, meta["source_spawn_id"])
assert row is not None
after = {
    "chat_id": row.chat_id,
    "harness": row.harness,
    "harness_session_id": row.harness_session_id,
    "model": row.model,
    "agent": row.agent,
    "work_id": row.work_id,
    "prompt": row.prompt,
}
assert after == before
print("PASS: source spawn/session metadata stayed unchanged")
PY
```

### FORK-12. Forking a nonexistent reference fails cleanly [IMPORTANT]

```bash
BAD_REF="fork-smoke-missing-$(date +%s)"
if uv run meridian spawn --fork "$BAD_REF" -p "missing fork smoke" >/tmp/meridian-fork-12.out 2>&1; then
  echo "FAIL: nonexistent fork ref unexpectedly succeeded"
elif grep -q "Traceback" /tmp/meridian-fork-12.out; then
  echo "FAIL: nonexistent fork ref produced a traceback"
elif grep -Eiq 'not found|cannot|unknown|missing|session' /tmp/meridian-fork-12.out; then
  echo "PASS: nonexistent fork ref failed with a clear message"
else
  echo "FAIL: nonexistent fork ref failed with unclear output"
fi
```

### FORK-13. Unsupported harness path fails clearly [IMPORTANT]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)"
if uv run meridian --harness definitely-not-a-harness spawn --fork "$SOURCE_SPAWN_ID" -p "bad harness smoke" >/tmp/meridian-fork-13.out 2>&1; then
  echo "FAIL: unsupported harness unexpectedly succeeded"
elif grep -q "Traceback" /tmp/meridian-fork-13.out; then
  echo "FAIL: unsupported harness produced a traceback"
elif grep -Eiq 'harness|unsupported|unknown' /tmp/meridian-fork-13.out; then
  echo "PASS: unsupported harness failed with a clear message"
else
  echo "FAIL: unsupported harness error text was unclear"
fi
```

### FORK-14. `--fork` + `--dry-run` previews without executing [CRITICAL]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
uv run meridian --json spawn --fork "$SOURCE_SPAWN_ID" -p "dry-run preview fork" --dry-run > /tmp/meridian-fork-14.json && \
uv run python - <<'PY'
import json
from pathlib import Path
doc = json.loads(Path("/tmp/meridian-fork-14.json").read_text(encoding="utf-8"))
assert doc["status"] == "dry-run"
assert isinstance(doc.get("cli_command"), list) and doc["cli_command"]
assert doc.get("forked_from")
assert "spawn_id" not in doc
print("PASS: fork dry-run returned preview data without launching")
PY
```

### FORK-15. Cross-harness fork is rejected [CRITICAL]

```bash
TARGET_HARNESS="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
source = (meta.get('source_harness') or '').strip()
for candidate in ('claude', 'codex', 'opencode'):
    if candidate and candidate != source:
        print(candidate)
        break
PY
)"
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)"
if uv run meridian --harness "$TARGET_HARNESS" spawn --fork "$SOURCE_SPAWN_ID" -p "cross harness fork" >/tmp/meridian-fork-15.out 2>&1; then
  echo "FAIL: cross-harness fork unexpectedly succeeded"
elif grep -q "Cannot fork across harnesses" /tmp/meridian-fork-15.out; then
  echo "PASS: cross-harness fork rejected with explicit message"
else
  echo "FAIL: cross-harness fork error text was unclear"
fi
```

### FORK-16. Forking a spawn without harness session id fails [CRITICAL]

```bash
MISSING_SPAWN_ID="$(uv run python - <<'PY'
import os
import time
from pathlib import Path
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

runtime_root = resolve_runtime_paths(Path(os.environ["MERIDIAN_PROJECT_DIR"])).root_dir
spawn_id = f"p{int(time.time())}"
spawn_store.start_spawn(
    runtime_root,
    spawn_id=spawn_id,
    chat_id="c900001",
    model="gpt-5.4",
    agent="reviewer",
    skills=(),
    harness="codex",
    prompt="missing harness session id seed",
    harness_session_id=None,
)
print(spawn_id)
PY
)"
if uv run meridian spawn --fork "$MISSING_SPAWN_ID" -p "should fail" >/tmp/meridian-fork-16.out 2>&1; then
  echo "FAIL: fork from missing harness session unexpectedly succeeded"
elif grep -q "has no recorded session" /tmp/meridian-fork-16.out; then
  echo "PASS: missing harness session id rejected cleanly"
else
  echo "FAIL: missing harness session id error text was unclear"
fi
```

### FORK-17. Legacy `--continue ... --fork` syntax gets helpful error [IMPORTANT]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)"
if uv run meridian spawn --continue "$SOURCE_SPAWN_ID" --fork "$SOURCE_SPAWN_ID" -p "legacy syntax" >/tmp/meridian-fork-17.out 2>&1; then
  echo "FAIL: legacy --continue + --fork unexpectedly succeeded"
elif grep -q "Cannot combine --fork with --continue" /tmp/meridian-fork-17.out; then
  echo "PASS: legacy syntax path shows the new conflict guidance"
else
  echo "FAIL: legacy syntax error message was unclear"
fi
```

### FORK-18. Raw harness session id forks with no meridian lineage [IMPORTANT]

```bash
SOURCE_HARNESS_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_harness_session_id'])
PY
)" && \
SOURCE_HARNESS="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_harness'])
PY
)" && \
uv run meridian --json --harness "$SOURCE_HARNESS" spawn --fork "$SOURCE_HARNESS_ID" -p "Raw harness fork." > /tmp/meridian-fork-18.json && \
uv run python - <<'PY'
import json
import os
from pathlib import Path
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_runtime_paths

source_harness_id = json.loads(Path("/tmp/meridian-fork-source-meta.json").read_text(encoding="utf-8"))["source_harness_session_id"]
doc = json.loads(Path("/tmp/meridian-fork-18.json").read_text(encoding="utf-8"))
fork_spawn_id = doc.get("spawn_id")
assert fork_spawn_id
assert doc.get("forked_from") == source_harness_id
runtime_root = resolve_runtime_paths(Path(os.environ["MERIDIAN_PROJECT_DIR"])).root_dir
row = spawn_store.get_spawn(runtime_root, fork_spawn_id)
assert row is not None and row.chat_id
records = session_store.get_session_records(runtime_root, {row.chat_id})
assert records
assert records[0].forked_from_chat_id is None
print("PASS: raw harness fork worked without session lineage")
PY
```

### FORK-19. Fork prompt guidance is used (not continuation guidance) [CRITICAL]

```bash
SOURCE_SPAWN_ID="$(uv run python - <<'PY'
import json
from pathlib import Path
meta = json.loads(Path('/tmp/meridian-fork-source-meta.json').read_text(encoding='utf-8'))
print(meta['source_spawn_id'])
PY
)" && \
uv run meridian --json spawn --fork "$SOURCE_SPAWN_ID" -p "Check fork guidance text." --dry-run > /tmp/meridian-fork-19.json && \
uv run python - <<'PY'
import json
from pathlib import Path
doc = json.loads(Path("/tmp/meridian-fork-19.json").read_text(encoding="utf-8"))
prompt = doc.get("composed_prompt", "")
assert "You are working in a forked Meridian session" in prompt
assert "You are resuming an existing Meridian session" not in prompt
print("PASS: fork guidance is present and continuation guidance is absent")
PY
```
