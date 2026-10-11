# Spawn return/report (live opt-in)

This is a small live boundary probe for foreground output, background wait, and
`MERIDIAN_TASK_DIR`. It is **not** part of automatic smoke: it launches a real
harness and may spend money. Use a disposable project and an eligible cheap
model only after deliberately supplying credentials.

The maintained output variants and report assertions live in
[`tests/integration/ops/test_spawn_execute_report_output.py`](../integration/ops/test_spawn_execute_report_output.py);
this guide only checks the real process boundary.

## Setup

```bash
. tests/smoke/scripts/setup.sh
smoke_add_agent test

# Set these explicitly for the live run; no default or paid model is selected
# for you.
export SMOKE_LIVE_HARNESS="${SMOKE_LIVE_HARNESS:?set an installed harness explicitly}"
export SMOKE_LIVE_MODEL="${SMOKE_LIVE_MODEL:?set an eligible cheap model explicitly}"
printf 'harness=%s model=%s (live/possibly billable)\n' "$SMOKE_LIVE_HARNESS" "$SMOKE_LIVE_MODEL"
```

`SMOKE_LIVE_HARNESS` must be an installed harness and
`SMOKE_LIVE_MODEL` must be eligible for that harness in the current catalog.
Do not substitute a fictional profile or alias. The shared setup keeps all
Meridian/native state under `SMOKE_ROOT`; copy selected auth into its isolated
store deliberately if needed.

## Foreground report

```bash
OUT=$(uv run meridian --format json spawn --harness "$SMOKE_LIVE_HARNESS" \
  -m "$SMOKE_LIVE_MODEL" -a test --timeout 1 -p 'Reply with exactly OK')
printf '%s\n' "$OUT"
SPAWN_ID=$(printf '%s' "$OUT" | uv run python -c \
  'import json,sys; p=json.load(sys.stdin); assert p["status"]=="succeeded", p; print(p["spawn_id"])')
uv run meridian spawn show "$SPAWN_ID" --format json
```

Require success and an `OK` reply; a provider/auth failure is not a passing
happy path. Check JSON report fields and the transcript hint in `show`.
Do not require token/cost fields: those are adapter-specific and covered by the
automated matrix.

## Background + wait

```bash
OUT=$(uv run meridian --format json spawn --bg --harness "$SMOKE_LIVE_HARNESS" \
  -m "$SMOKE_LIVE_MODEL" -a test --timeout 1 -p 'Reply with exactly OK')
printf '%s\n' "$OUT"
SPAWN_ID=$(printf '%s' "$OUT" | uv run python -c \
  'import json,sys; print(json.load(sys.stdin)["spawn_id"])')
uv run meridian spawn wait "$SPAWN_ID"
```

Check submission returns a wait-required/background status and wait returns one
successful terminal report with `OK`. Drain the wait before leaving the shell.

## Task-directory probe

```bash
TASK_DIR="$SMOKE_ROOT/task"
mkdir -p "$TASK_DIR"
uv run meridian --format json spawn --harness "$SMOKE_LIVE_HARNESS" \
  -m "$SMOKE_LIVE_MODEL" -a test --timeout 1 --task-dir "$TASK_DIR" \
  -p 'Print pwd and MERIDIAN_TASK_DIR, then report both values exactly.'
```

The report should show the harness process's project/control cwd while
`MERIDIAN_TASK_DIR` names `TASK_DIR`. After inspection and draining all runs,
call `smoke_cleanup` to remove the entire owned fixture.
