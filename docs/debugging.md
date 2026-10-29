# Debugging meridian state

Read this only when something looks wrong: a spawn seems stuck, expected output is missing, `show` and `wait` disagree with what you expected, or you need to inspect low-level state.

## Common Failure Modes

| Symptom | Likely cause | First move |
|---|---|---|
| `crashed` | The runner's lifetime lock was freed before it wrote an outcome | Check `report.md`; the work may still be usable. Relaunch or use `--continue`. A pending cancel request produces `cancelled` instead. |
| `missing_spawn_dir` | Crash during launch before spawn artifacts stabilized | Relaunch the spawn |
| Exit 127 or 2 with empty report | Harness binary missing from `$PATH` | Install or fix PATH for the selected harness |
| Exit 143 or 137 | Process terminated externally | Check `meridian spawn show <id>` first; if status is `succeeded`, signal hit during cleanup and no retry is needed. Otherwise check host logs for OOM or external kill, then retry |
| `timed_out` status | Runtime budget exceeded | Increase timeout or split task into smaller spawns |
| Model/API error in `stderr.log` | Model unavailable or API rejected request | Check `meridian mars models list` and provider credentials |

## Spawn Artifact Layout

Each spawn writes artifacts under `~/.meridian/projects/<project-id>/spawns/<spawn-id>/`. Spawns created by v0.18 and later keep each run's files (prompt, transcript, report, logs) in `launches/<n>/` inside that directory, and the spawn-level files (`state.json`, `launch.lock`, `scope.json`) at the top; older spawns keep a run's files at the top too. Use CLI output (`meridian spawn show`, `meridian spawn report show`) rather than hardcoding paths.

| File | Contents |
|---|---|
| `state.json` | Authoritative spawn record (status, metrics, error) |
| `report.md` | Final report when the spawn reached report emission |
| `output.jsonl` | Raw harness stdout (prefer `meridian spawn log`) |
| `stderr.log` | Harness stderr, warnings, and errors |

## First Checks

Start with the normal read path:

```bash
meridian spawn show SPAWN_ID
meridian spawn wait SPAWN_ID
meridian spawn list --all
```

Use `show` first to inspect the persisted state for one spawn. Use `wait` if the spawn may still be running. Use `list --all` for situational awareness, not as your primary lookup path.

## Logs

Each run of a spawn keeps its own stderr log in `launches/<n>/stderr.log` inside the spawn's directory (older spawns keep it at the top of that directory). Find the runtime root with `meridian doctor` (`runtime_root:`), then list the latest run's files:

```bash
meridian doctor
ls <runtime_root>/spawns/SPAWN_ID/launches/
```

Tail a running log only when you need detailed harness traces:

```bash
tail -f <runtime_root>/spawns/SPAWN_ID/launches/N/stderr.log
```

To read what a run reported rather than how it ran, use `meridian spawn report show SPAWN_ID`.
After a continue, that shows the latest run. An earlier run's report is found by
`meridian spawn report search QUERY`, and its conversation by `meridian session log SPAWN_ID`.

## Shared Files

If a workflow depends on handoff files, inspect the knowledge base directory.

```bash
ls -la "$(meridian context kb)"
```

## Stuck Spawn Recovery

Meridian derives running-spawn status on read. If a runner dies without writing an outcome, `spawn list`, `spawn show`, and `spawn wait` immediately report `failed`/`crashed`; a pending cancel request reports `cancelled`. Plain `meridian doctor` lists such spawns under `dead_spawns_unfinalized`; `meridian doctor --kill-orphans` persists the outcome and cleans up recorded process scopes.

A `running` spawn whose runner is alive is not stuck while it drains and writes its report. If the runner's lifetime lock is freed before an outcome is written, the spawn reads as `failed`/`crashed` (or `cancelled` when cancellation was requested). Check `report.md`; it may still contain useful work, but it does not determine the outcome.

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
