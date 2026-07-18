# Project resolution smoke tests

Manual smoke guide for global `-C` / `--directory` project targeting and stale
runtime-env cleanup.

## Setup

```bash
export REPO_ROOT="$(pwd)"
export SMOKE_A="$(mktemp -d /tmp/meridian-proj-a.XXXXXX)"
export SMOKE_B="$(mktemp -d /tmp/meridian-proj-b.XXXXXX)"
export MERIDIAN_HOME="$(mktemp -d /tmp/meridian-home.XXXXXX)"
for var in $(env | awk -F= '/^MERIDIAN_/ {print $1}'); do
  case "$var" in MERIDIAN_HOME) ;; *) unset "$var" ;; esac
done
mkdir -p "$SMOKE_A/.meridian" "$SMOKE_B/.meridian"
printf 'proj-a\n' > "$SMOKE_A/.meridian/id"
printf 'proj-b\n' > "$SMOKE_B/.meridian/id"

uv run python - <<'PY'
from meridian.lib.state.spawn_store import start_spawn
from meridian.lib.state.user_paths import get_project_home

start_spawn(
    get_project_home("proj-a"),
    spawn_id="p-smoke-a",
    chat_id="c-smoke-a",
    model="smoke",
    agent="qa",
    harness="codex",
    prompt="project a",
)
start_spawn(
    get_project_home("proj-b"),
    spawn_id="p-smoke-b",
    chat_id="c-smoke-b",
    model="smoke",
    agent="qa",
    harness="codex",
    prompt="project b",
)
PY
```

## PROJECT-1. `-C` wins over stale inherited runtime state [CRITICAL]

```bash
export MERIDIAN_PROJECT_DIR="$SMOKE_A"
export _MERIDIAN_RUNTIME_DIR="$MERIDIAN_HOME/projects/proj-a"
uv run meridian -C "$SMOKE_B" --json spawn list > /tmp/meridian-project-c.json && \
uv run python - <<'PY'
import json

payload = json.load(open("/tmp/meridian-project-c.json", encoding="utf-8"))
spawn_ids = {row["spawn_id"] for row in payload["spawns"]}
assert "p-smoke-b" in spawn_ids, spawn_ids
assert "p-smoke-a" not in spawn_ids, spawn_ids
PY
echo "PASS: -C ignored stale _MERIDIAN_RUNTIME_DIR and read target project state"
```

## PROJECT-2. `MERIDIAN_PROJECT_DIR` remains the inherited project source [CRITICAL]

```bash
unset _MERIDIAN_RUNTIME_DIR
export MERIDIAN_PROJECT_DIR="$SMOKE_A"
uv run meridian --json spawn list > /tmp/meridian-project-env.json && \
uv run python - <<'PY'
import json

payload = json.load(open("/tmp/meridian-project-env.json", encoding="utf-8"))
spawn_ids = {row["spawn_id"] for row in payload["spawns"]}
assert "p-smoke-a" in spawn_ids, spawn_ids
assert "p-smoke-b" not in spawn_ids, spawn_ids
PY
echo "PASS: MERIDIAN_PROJECT_DIR selected inherited project state"
```

## Cleanup

```bash
rm -rf "$SMOKE_A" "$SMOKE_B" "$MERIDIAN_HOME" \
  /tmp/meridian-project-c.json \
  /tmp/meridian-project-env.json
unset SMOKE_A SMOKE_B MERIDIAN_HOME MERIDIAN_PROJECT_DIR _MERIDIAN_RUNTIME_DIR REPO_ROOT
echo "PASS: cleanup complete"
```
