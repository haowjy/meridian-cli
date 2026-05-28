# State Integrity

Run these checks after smoke flows that create `.meridian/` runtime state. The v2 spawn store is file-authoritative: live spawn state is kept in `spawns/<id>/state.json`, with `starting-prompt.md` as prompt-body authority and `spawns/v2-format.json` as the v2 marker.

## Setup

```bash
export REPO_ROOT=/abs/path/to/meridian-channel
export SMOKE_REPO="$(mktemp -d /tmp/meridian-state.XXXXXX)"
git -C "$SMOKE_REPO" init --quiet
for var in $(env | awk -F= '/^MERIDIAN_/ {print $1}'); do unset "$var"; done
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
cd "$REPO_ROOT"
export RUNTIME_ROOT="$(uv run python - <<'PY'
import os
from pathlib import Path
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.user_paths import get_or_create_project_id, get_project_home

state_dir = resolve_project_paths(Path(os.environ["SMOKE_REPO"])).root_dir
print(get_project_home(get_or_create_project_id(state_dir)))
PY
)"
mkdir -p "$RUNTIME_ROOT/spawns"
cd "$REPO_ROOT"
uv run meridian spawn -h >/dev/null 2>&1 && echo "PASS: state fixture setup complete" || echo "FAIL: state fixture setup failed"
```

### STATE-1. Core runtime directories exist [CRITICAL]

```bash
test -d "$RUNTIME_ROOT" && \
test -d "$RUNTIME_ROOT/spawns" && \
echo "PASS: runtime root and spawns directory exist" || echo "FAIL: runtime state directories are incomplete"
```

### STATE-2. Spawn v2 marker and `state.json` are valid [IMPORTANT]

```bash
uv run python - <<'PY'
import json, os
from pathlib import Path
from meridian.lib.state.spawn_store import start_spawn

root = Path(os.environ["RUNTIME_ROOT"])
spawn_id = str(
    start_spawn(
        root,
        chat_id="c-state",
        model="gpt-5.4",
        agent="smoke",
        harness="codex",
        prompt="hello state",
        status="running",
    )
)
marker = json.loads((root / "spawns" / "v2-format.json").read_text(encoding="utf-8"))
state = json.loads((root / "spawns" / spawn_id / "state.json").read_text(encoding="utf-8"))
assert marker["v"] == 2
assert state["v"] == 2
assert state["id"] == spawn_id
assert state["status"] == "running"
assert state["prompt_length"] == len("hello state")
assert "hello state" not in json.dumps(state)
print("PASS: v2 marker and state.json are well-formed")
PY
```

### STATE-3. `sessions.jsonl` is valid when present [IMPORTANT]

```bash
uv run python - <<'PY'
import json, os
path = os.path.join(os.environ["RUNTIME_ROOT"], "sessions.jsonl")
if not os.path.exists(path):
    print("PASS: sessions.jsonl has not been created yet")
else:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                assert isinstance(json.loads(line), dict)
    print("PASS: sessions.jsonl is well-formed")
PY
```

### STATE-4. Live lock files are acquirable after smoke setup [IMPORTANT]

```bash
uv run python - <<'PY'
import os
import pathlib
import sys

root = pathlib.Path(os.environ["RUNTIME_ROOT"])
lock_paths = sorted(root.glob("spawns/*/state.lock")) + sorted(root.glob("spawns/migration.lock"))
if os.name == "nt":
    print("PASS: skipping POSIX flock probe on Windows")
elif not lock_paths:
    print("PASS: no per-spawn locks present yet")
else:
    import fcntl
    for path in lock_paths:
        with path.open("a+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    print("PASS: per-spawn lock files are acquirable")
PY
```

### STATE-5. No stale atomic temp files remain [NICE-TO-HAVE]

```bash
if find "$RUNTIME_ROOT" -name '.*.tmp' -print | grep -q .; then
  echo "FAIL: stale atomic temp files remain"
else
  echo "PASS: no stale atomic temp files remain"
fi
```

### STATE-6. Dead runner + stale heartbeat (>120s) stamps `orphan_run` in `state.json` [CRITICAL]

```bash
uv run python - <<'PY'
import json, os, pathlib, subprocess, time
from meridian.lib.state.spawn_store import start_spawn

root = pathlib.Path(os.environ["RUNTIME_ROOT"])
spawn_id = "p-orphan-heartbeat-smoke"
start_spawn(
    root,
    spawn_id=spawn_id,
    chat_id="c-state",
    model="gpt-5.4",
    agent="smoke",
    harness="codex",
    prompt="state integrity smoke",
    status="running",
    runner_pid=999999,
    started_at="2000-01-01T00:00:00Z",
)
spawn_dir = root / "spawns" / spawn_id
spawn_dir.mkdir(parents=True, exist_ok=True)
heartbeat = spawn_dir / "heartbeat"
heartbeat.touch(exist_ok=True)
stale_epoch = time.time() - 180
os.utime(heartbeat, (stale_epoch, stale_epoch))

subprocess.run(
    ["uv", "run", "meridian", "spawn", "show", spawn_id],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

state = json.loads((spawn_dir / "state.json").read_text(encoding="utf-8"))
assert state["status"] == "failed"
assert state["error"] == "orphan_run"
assert state["terminal_origin"] == "reconciler"
assert (root / "spawns" / "v2-format.json").is_file()
print("PASS: stale heartbeat triggered orphan_run reconciliation in state.json")
PY
```

### STATE-7. Cancel path records `terminal_origin="cancel"` in `state.json` [CRITICAL]

```bash
uv run python - <<'PY'
import json, os, pathlib, subprocess, uuid
from meridian.lib.state.spawn_store import start_spawn

root = pathlib.Path(os.environ["RUNTIME_ROOT"])
spawn_id = f"p-origin-cancel-smoke-{uuid.uuid4().hex[:8]}"
start_spawn(
    root,
    spawn_id=spawn_id,
    chat_id="c-state",
    model="gpt-5.4",
    agent="smoke",
    harness="codex",
    prompt="cancel origin smoke",
    status="running",
)

subprocess.run(
    ["uv", "run", "meridian", "spawn", "cancel", spawn_id],
    check=True,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

state = json.loads((root / "spawns" / spawn_id / "state.json").read_text(encoding="utf-8"))
assert state["status"] == "cancelled"
assert state["terminal_origin"] == "cancel"
assert state["exit_code"] == 130
print("PASS: cancel flow persisted terminal_origin=cancel in state.json")
PY
```

### STATE-8. Success path records `terminal_origin="runner"` [IMPORTANT]

```bash
uv run python - <<'PY'
import json, os
from pathlib import Path
from meridian.lib.state.spawn_store import finalize_spawn, start_spawn

root = Path(os.environ["RUNTIME_ROOT"])
spawn_id = str(
    start_spawn(
        root,
        chat_id="c-success",
        model="gpt-5.4",
        agent="smoke",
        harness="codex",
        prompt="success origin smoke",
        status="running",
    )
)
finalize_spawn(root, spawn_id, status="succeeded", exit_code=0, origin="runner")
state = json.loads((root / "spawns" / spawn_id / "state.json").read_text(encoding="utf-8"))
assert state["status"] == "succeeded"
assert state["terminal_origin"] == "runner"
print("PASS: success finalize recorded terminal_origin=runner")
PY
```

### STATE-9. Stale `finalizing` heartbeat stamps `orphan_finalization` [CRITICAL]

```bash
uv run python - <<'PY'
import json, os, pathlib, subprocess, time
from meridian.lib.state.spawn_store import start_spawn

root = pathlib.Path(os.environ["RUNTIME_ROOT"])
spawn_id = "p-orphan-finalizing-stale-smoke"
start_spawn(
    root,
    spawn_id=spawn_id,
    chat_id="c-state",
    model="gpt-5.4",
    agent="smoke",
    harness="codex",
    prompt="finalizing stale heartbeat smoke",
    status="finalizing",
    runner_pid=999999,
    started_at="2000-01-01T00:00:00Z",
)
spawn_dir = root / "spawns" / spawn_id
spawn_dir.mkdir(parents=True, exist_ok=True)
heartbeat = spawn_dir / "heartbeat"
heartbeat.touch(exist_ok=True)
stale_epoch = time.time() - 180
os.utime(heartbeat, (stale_epoch, stale_epoch))

subprocess.run(
    ["uv", "run", "meridian", "spawn", "show", spawn_id],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

state = json.loads((spawn_dir / "state.json").read_text(encoding="utf-8"))
assert state["status"] == "failed"
assert state["error"] == "orphan_finalization"
assert state["terminal_origin"] == "reconciler"
print("PASS: stale finalizing heartbeat triggered orphan_finalization in state.json")
PY
```

### STATE-10. Recent `finalizing` heartbeat keeps the row non-terminal [IMPORTANT]

```bash
uv run python - <<'PY'
import json, os, pathlib, subprocess, time
from meridian.lib.state.spawn_store import start_spawn

root = pathlib.Path(os.environ["RUNTIME_ROOT"])
spawn_id = "p-orphan-finalizing-fresh-smoke"
start_spawn(
    root,
    spawn_id=spawn_id,
    chat_id="c-state",
    model="gpt-5.4",
    agent="smoke",
    harness="codex",
    prompt="finalizing fresh heartbeat smoke",
    status="finalizing",
    runner_pid=999999,
    started_at="2000-01-01T00:00:00Z",
)
spawn_dir = root / "spawns" / spawn_id
spawn_dir.mkdir(parents=True, exist_ok=True)
heartbeat = spawn_dir / "heartbeat"
heartbeat.touch(exist_ok=True)
fresh_epoch = time.time() - 5
os.utime(heartbeat, (fresh_epoch, fresh_epoch))

subprocess.run(
    ["uv", "run", "meridian", "spawn", "show", spawn_id],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

state = json.loads((spawn_dir / "state.json").read_text(encoding="utf-8"))
assert state["status"] == "finalizing"
assert state["finished_at"] is None
assert state["terminal_origin"] is None
print("PASS: recent finalizing heartbeat kept the row non-terminal")
PY
```
