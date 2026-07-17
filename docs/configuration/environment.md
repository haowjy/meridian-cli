# Environment variables

## Project root and task directory

Project root (control root for config, runtime state, and harness context) resolves
in this order:

1. `-C` / `--directory` (CLI flag)
2. `MERIDIAN_PROJECT_DIR` (inherited across spawns)
3. Literal process CWD (no marker walk-up)

Commands that require an established project error when only bare CWD applies —
run from the project root, pass `-C <path>`, or set `MERIDIAN_PROJECT_DIR`.

Task directory (where source reads, edits, git, builds, and tests run) is separate.
`MERIDIAN_TASK_DIR` is inherited across spawns. Precedence within a session:
spawn-scope override (`meridian task-dir set`, stored in per-spawn `scope.json`) →
work-item `task_dir` (`meridian work task-dir`) → inherited `MERIDIAN_TASK_DIR` →
project root. Query with `meridian task-dir`; clear a stale inherited value with
`meridian task-dir clear`. Use `--task-dir` on primary/spawn launch for one-shot
overrides — not `-C`, which retargets the entire project/control root.

`MERIDIAN_PROJECT_ROOT` is the bind-time export of the resolved project/control
root to child sessions. `MERIDIAN_TASK_CWD` is a bind-time alias for the child's
resolved logical task cwd; it is not inherited.

## Core

| Variable | Purpose |
|---|---|
| `MERIDIAN_PROJECT_DIR` | Inherited project/control root; wins over literal CWD when set |
| `MERIDIAN_PROJECT_ROOT` | Bind-time export of project/control root to child sessions |
| `MERIDIAN_TASK_DIR` | Inherited source-edit directory; set/clear via `meridian task-dir` |
| `MERIDIAN_TASK_CWD` | Bind-time alias for the child's resolved task cwd (not inherited) |
| `MERIDIAN_CONFIG` | User config overlay path |
| `MERIDIAN_HOME` | Override user state root (default `~/.meridian/`) |
| `MERIDIAN_RUNTIME_DIR` | Override the runtime state root. Absolute path = use as-is; relative path = resolve relative to repo root. Repo-owned default paths (`kb/`, `work/`, `archive/work/`) always stay in `.meridian/` regardless of this setting. |
| `MERIDIAN_FS_DIR` | Resolved shared filesystem path for the current repo state root |
| `MERIDIAN_ACTIVE_WORK_ID` | Active attached work item slug, when one exists |
| `MERIDIAN_ACTIVE_WORK_DIR` | Active scope directory: named work item scratch dir when attached, else the run's ambient spawn scope (`spawns/p<N>/work`) |
| `MERIDIAN_SPAWN_ID` | Current run/spawn ID for primary and delegated execution |
| `MERIDIAN_CHAT_ID` | Top-level session id inherited across the spawn tree |
| `MERIDIAN_DEPTH` | Zero-based delegation depth (`0` = primary/root, `1` = first delegated spawn) |
| `MERIDIAN_MAX_DEPTH` | Max zero-based delegated spawn depth override |
| `MERIDIAN_PARENT_SPAWN_ID` | Immediate parent spawn ID for nested execution |

## Runtime Policy Overrides

These override spawn-level runtime policy. They sit above project config but below CLI flags in the precedence chain and apply independently of agent overlays.

| Variable | Purpose |
|---|---|
| `MERIDIAN_MODEL` | Model for this spawn (alias or canonical ID) |
| `MERIDIAN_EFFORT` | Reasoning effort: `low`, `medium`, `high`, `xhigh`, `max` |
| `MERIDIAN_APPROVAL` | Approval mode: `default`, `confirm`, `auto`, `yolo` |
| `MERIDIAN_SANDBOX` | Sandbox level |
| `MERIDIAN_AUTOCOMPACT` | Context compaction threshold (int 1–100) |
| `MERIDIAN_TIMEOUT` | Spawn timeout in minutes (float > 0) |
| `MERIDIAN_RESIDENT_REARM_BUDGET` | Maximum resident deadline extensions (int >= 0; unset is unlimited) |

## Config Overrides

- `MERIDIAN_MAX_RETRIES`
- `MERIDIAN_RETRY_BACKOFF_SECONDS`
- `MERIDIAN_KILL_GRACE_MINUTES`
- `MERIDIAN_GUARDRAIL_TIMEOUT_MINUTES`
- `MERIDIAN_WAIT_TIMEOUT_MINUTES`
- `MERIDIAN_RESIDENT_REARM_BUDGET`
- `MERIDIAN_HARNESS_MODEL_CLAUDE`
- `MERIDIAN_HARNESS_MODEL_CODEX`
- `MERIDIAN_HARNESS_MODEL_OPENCODE`
- `MERIDIAN_STATE_RETENTION_DAYS`

## Guardrails and Secrets

| Variable | Purpose |
|---|---|
| `MERIDIAN_GUARDRAIL_RUN_ID` | Spawn id passed to guardrail scripts |
| `MERIDIAN_GUARDRAIL_OUTPUT_LOG` | Path to `output.jsonl` |
| `MERIDIAN_GUARDRAIL_REPORT_PATH` | Path to `report.md` when a report exists |
| `MERIDIAN_SECRET_<KEY>` | Secret injection/redaction channel |

## Permission Naming

Use `workspace-write` (not `space-write`) as the writable middle tier.
