# OpenCode Orphan Cleanup

Validate the real managed-backend failure that does not belong in the automated
unit/integration suite: an OpenCode backend must not survive a hard-crashed
Meridian worker, and the reaper must converge the spawn to a terminal state.

This guide intentionally launches a real OpenCode backend and kills the
Meridian runner process. Run it manually, serially, and only on a machine where
OpenCode is installed and disposable test state is acceptable.

## Setup

```bash
export REPO_ROOT=/abs/path/to/meridian-cli
export SMOKE_REPO="$(mktemp -d /tmp/meridian-opencode-orphan.XXXXXX)"
git -C "$SMOKE_REPO" init --quiet
for var in $(env | awk -F= '/^MERIDIAN_/ {print $1}'); do unset "$var"; done
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
cd "$REPO_ROOT"
export RUNTIME_ROOT="$(uv run python tests/e2e/resolve-runtime-root.py)"
mkdir -p "$RUNTIME_ROOT/spawns" "$SMOKE_REPO/.mars/agents"
cat > "$SMOKE_REPO/.mars/agents/reviewer.md" <<'AGENT'
# Reviewer

You are a smoke-test reviewer. Wait briefly, then reply with one short sentence.
AGENT
command -v opencode >/dev/null && echo "PASS: opencode available" || echo "FAIL: opencode missing"
```

### OCO-1. Launch a real OpenCode background spawn [CRITICAL]

```bash
cd "$REPO_ROOT"
uv run meridian --json spawn -a reviewer --harness opencode --bg \
  -p "OpenCode orphan cleanup smoke: keep the backend busy for at least 60 seconds before answering. If you can run shell commands, run sleep 60 first." \
  > /tmp/meridian-opencode-orphan-create.json

uv run python - <<'PY'
import json
doc = json.load(open('/tmp/meridian-opencode-orphan-create.json', encoding='utf-8'))
spawn_id = doc.get('spawn_id') or doc.get('id')
assert spawn_id, doc
print(f"PASS: created {spawn_id}")
PY
```

### OCO-2. Wait for runner and backend facts [CRITICAL]

```bash
export SPAWN_ID="$(uv run python - <<'PY'
import json
doc = json.load(open('/tmp/meridian-opencode-orphan-create.json', encoding='utf-8'))
print(doc.get('spawn_id') or doc.get('id'))
PY
)"

uv run python - <<'PY'
import json, os, time
from pathlib import Path

root = Path(os.environ["RUNTIME_ROOT"])
spawn_id = os.environ["SPAWN_ID"]
spawn_dir = root / "spawns" / spawn_id
state_path = spawn_dir / "state.json"
lifecycle_path = spawn_dir / "backend_lifecycle.json"
deadline = time.time() + 60

while time.time() < deadline:
    if state_path.is_file() and lifecycle_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        runner_pid = state.get("runner_pid")
        backend_pid = lifecycle.get("backend_pid")
        if isinstance(runner_pid, int) and runner_pid > 0 and isinstance(backend_pid, int) and backend_pid > 0:
            print(f"RUNNER_PID={runner_pid}")
            print(f"BACKEND_PID={backend_pid}")
            print("PASS: runner and backend facts recorded")
            raise SystemExit(0)
    time.sleep(0.5)

raise SystemExit("FAIL: timed out waiting for runner/backend facts")
PY
```

### OCO-3. Hard-crash the runner and trigger reconciliation [CRITICAL]

```bash
eval "$(uv run python - <<'PY'
import json, os, shlex, sys
from pathlib import Path

import psutil

root = Path(os.environ["RUNTIME_ROOT"])
spawn_id = os.environ["SPAWN_ID"]
state = json.loads((root / "spawns" / spawn_id / "state.json").read_text(encoding="utf-8"))
lifecycle = json.loads((root / "spawns" / spawn_id / "backend_lifecycle.json").read_text(encoding="utf-8"))
status = state.get("status")
runner_pid = state.get("runner_pid")
runner_birth = state.get("runner_created_at_epoch")
backend_pid = lifecycle.get("backend_pid")

if status not in {"queued", "running", "finalizing"}:
    raise SystemExit(f"FAIL: spawn already terminal before crash injection: {status}")
if not isinstance(runner_pid, int) or runner_pid <= 0:
    raise SystemExit(f"FAIL: invalid runner_pid before crash injection: {runner_pid!r}")
if not isinstance(backend_pid, int) or backend_pid <= 0:
    raise SystemExit(f"FAIL: invalid backend_pid before crash injection: {backend_pid!r}")

proc = psutil.Process(runner_pid)
if isinstance(runner_birth, (int, float)) and abs(proc.create_time() - float(runner_birth)) > 1.0:
    raise SystemExit(
        f"FAIL: runner PID birth time mismatch before kill; refusing to signal reused pid {runner_pid}"
    )

print(f"RUNNER_PID={shlex.quote(str(runner_pid))}")
print(f"BACKEND_PID={shlex.quote(str(backend_pid))}")
print(f"PASS: validated live runner {runner_pid}; ready for crash injection", file=sys.stderr)
PY
)"

kill -9 "$RUNNER_PID"
sleep 2

# A read path should run the reaper in the root side-effect process.
uv run meridian --json spawn show "$SPAWN_ID" > /tmp/meridian-opencode-orphan-show.json || true
```

### OCO-4. Spawn finalizes and backend is gone [CRITICAL]

```bash
uv run python - <<'PY'
import json, os, signal, subprocess, time
from pathlib import Path

root = Path(os.environ["RUNTIME_ROOT"])
spawn_id = os.environ["SPAWN_ID"]
backend_pid = int(os.environ["BACKEND_PID"])
deadline = time.time() + 30

def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

latest = None
while time.time() < deadline:
    subprocess.run(
        ["uv", "run", "meridian", "--json", "spawn", "show", spawn_id],
        cwd=os.environ["REPO_ROOT"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    state = json.loads((root / "spawns" / spawn_id / "state.json").read_text(encoding="utf-8"))
    latest = state
    if state.get("status") in {"failed", "cancelled", "succeeded"} and not alive(backend_pid):
        print(f"PASS: spawn finalized as {state.get('status')} and backend {backend_pid} is gone")
        raise SystemExit(0)
    time.sleep(1)

raise SystemExit(f"FAIL: did not converge; latest={latest!r}; backend_alive={alive(backend_pid)}")
PY
```

## Cleanup

```bash
rm -rf "$SMOKE_REPO"
```
