# Debugging meridian state

Read this only when something looks wrong: a spawn seems stuck, expected output is missing, `show` and `wait` disagree with what you expected, or you need to inspect low-level state.

## Common Failure Modes

| Symptom | Likely cause | First move |
|---|---|---|
| `orphan_run` | Runner process died while `running` without ever reaching `finalizing` | Relaunch after read-path reconciliation updates the record; no report will be present |
| `orphan_finalization` | Runner reached `finalizing` but was abandoned before writing terminal state | Check `spawn show` — `report.md` may still hold useful content the harness produced before exit |
| `missing_runner_pid` | Runner PID was never recorded (harness crashed before startup stabilized) | Confirm harness binaries are installed and on `$PATH`; relaunch |
| `missing_spawn_dir` | Crash during launch before spawn artifacts stabilized | Relaunch the spawn |
| Exit 127 or 2 with empty report | Harness binary missing from `$PATH` | Install or fix PATH for the selected harness |
| Exit 143 or 137 | Process terminated externally | Check `meridian spawn show <id>` first; if status is `succeeded`, signal hit during cleanup and no retry is needed. Otherwise check host logs for OOM or external kill, then retry |
| `timed_out` status | Runtime budget exceeded | Increase timeout or split task into smaller spawns |
| Model/API error in `stderr.log` | Model unavailable or API rejected request | Check `meridian mars models list` and provider credentials |

## Spawn Artifact Layout

Each spawn writes artifacts under `.meridian/spawns/<spawn_id>/`.

| File | Contents |
|---|---|
| `state.json` | Authoritative spawn record (status, metrics, error) |
| `report.md` | Final report when the spawn reached report emission |
| `output.jsonl` | Raw harness stdout (prefer `meridian spawn log`) |
| `stderr.log` | Harness stderr, warnings, and errors |
| `prompt.md` | Prompt materialized for the harness |
| `harness.pid` | Foreground harness PID file |
| `heartbeat` | Liveness touch file while spawn is active |

## First Checks

Start with the normal read path:

```bash
meridian spawn show SPAWN_ID
meridian spawn wait SPAWN_ID
meridian spawn list --all
```

Use `show` first to inspect the persisted state for one spawn. Use `wait` if the spawn may still be running. Use `list --all` for situational awareness, not as your primary lookup path.

## Logs

Each spawn records a stderr log under the Meridian state root. Prefer getting the path from `spawn show` output instead of constructing it manually.

```bash
meridian spawn show SPAWN_ID
# Look for "log_path" in the JSON output.
```

If you already know the resolved state root, the log is typically at:

```text
$MERIDIAN_STATE_ROOT/spawns/SPAWN_ID/stderr.log
```

Tail a running log only when you need detailed harness traces:

```bash
tail -f "$MERIDIAN_STATE_ROOT/spawns/SPAWN_ID/stderr.log"
```

## Shared Files

If a workflow depends on handoff files, inspect the knowledge base directory.

```bash
ls -la "$(meridian context kb)"
```

## Stuck Spawn Recovery

Meridian reconciles running spawns on read. If a process died, wrote a report without finalizing, or went stale, the next `spawn list`, `spawn show`, `spawn wait`, or `meridian doctor` call detects that and marks it failed.

A spawn in `finalizing` status is **not stuck** — it is in the drain/report/final-status window after the runner has recorded its resolved exit tuple. Give it a few seconds before treating it as stuck. If a `finalizing` record persists with no recent activity, reconciliation uses any durable report or recorded runner-exit tuple first; if neither can prove completion, it moves the spawn to `failed` with `orphan_finalization`.

If a spawn seems stuck, run:

```bash
meridian spawn list --all
meridian doctor
```

You do not need to manually clean up background processes or edit state files.

## State Layout

State layout is for debugging only:

- `MERIDIAN_STATE_ROOT/spawns/<id>/state.json`: authoritative per-spawn record (status, metrics, error)
- `MERIDIAN_STATE_ROOT/spawns/<id>/`: per-spawn artifacts and logs
- `MERIDIAN_STATE_ROOT/spawns/v2-format.json`: migration marker — present means v2 format is active
- `MERIDIAN_STATE_ROOT/spawns.legacy-v1.jsonl`: archived v1 event log (safe to delete once confirmed stable)
- KB dir (`meridian context kb`): shared knowledge base between spawns

Prefer CLI reads and environment variables over hardcoded `.meridian/...` paths.
