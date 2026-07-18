# Environment variables

## Public and internal namespaces

`MERIDIAN_*` is Meridian's public environment contract. Users, prompt packages,
agents, hook scripts, and companion tools may set or read these names according
to their role. Public names are stable and fall into four groups:

- **config inputs** such as `MERIDIAN_MODEL` and `MERIDIAN_MAX_DEPTH`;
- **injected handles** such as `MERIDIAN_PROJECT_DIR`, `MERIDIAN_TASK_DIR`, and
  `MERIDIAN_SPAWN_ID`;
- **hook payload** names (`MERIDIAN_HOOK_*`, `MERIDIAN_SPAWN_STATUS`,
  `MERIDIAN_SPAWN_AGENT`, `MERIDIAN_SPAWN_MODEL`,
  `MERIDIAN_SPAWN_DURATION_SECS`, `MERIDIAN_SPAWN_COST_USD`, and
  `MERIDIAN_SPAWN_ERROR`); and
- **inter-tool signals**, currently `MERIDIAN_MANAGED`.

`_MERIDIAN_*` is repo-internal process plumbing. These values are documented
only for contributors and diagnostics; external callers must not set or depend
on them, and their names may change without notice. The source registry in
`src/meridian/env_registry.py` is authoritative for both tiers.

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

## Core

| Variable | Purpose |
|---|---|
| `MERIDIAN_PROJECT_DIR` | Inherited project/control root; wins over literal CWD when set |
| `MERIDIAN_TASK_DIR` | Inherited source-edit directory; set/clear via `meridian task-dir` |
| `MERIDIAN_CONFIG` | User config overlay path |
| `MERIDIAN_HOME` | Override user state root (default `~/.meridian/`) |
| `MERIDIAN_ACTIVE_WORK_ID` | Active attached work item slug, when one exists |
| `MERIDIAN_ACTIVE_WORK_DIR` | Active scope directory: named work item scratch dir when attached, else the run's ambient spawn scope (`spawns/p<N>/work`) |
| `MERIDIAN_SPAWN_ID` | Current run/spawn ID for primary and delegated execution |
| `MERIDIAN_CHAT_ID` | Top-level session id inherited across the spawn tree |
| `MERIDIAN_MAX_DEPTH` | Max zero-based delegated spawn depth override |

Internally, `_MERIDIAN_DEPTH` carries the current zero-based counter while the
public `MERIDIAN_MAX_DEPTH` remains the user-configurable cap. Other internal
handles include the resolved runtime directory, parent spawn ID, harness ID,
guardrail paths, and Pi transport settings; consult the registry rather than
depending on those names.

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
| `_MERIDIAN_GUARDRAIL_RUN_ID` | Spawn id passed to guardrail scripts |
| `_MERIDIAN_GUARDRAIL_OUTPUT_LOG` | Path to `output.jsonl` |
| `_MERIDIAN_GUARDRAIL_REPORT_PATH` | Path to `report.md` when a report exists |
| `MERIDIAN_SECRET_<KEY>` | Secret injection/redaction channel |

## Permission Naming

Use `workspace-write` (not `space-write`) as the writable middle tier.
