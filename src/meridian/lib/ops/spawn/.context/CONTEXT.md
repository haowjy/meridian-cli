# ops/spawn — Contracts and Architecture

## Depth Guard

`spawn_create_sync()` checks `max_depth_reached()` **before** executing any spawn. When depth
is exceeded, it returns `depth_exceeded_output()` with no side effects — no spawn row is created.

Depth is read from `MERIDIAN_DEPTH` env var in the child. The reaper also checks `MERIDIAN_DEPTH`
and skips reaping when inside a spawn. This is an ops-layer enforcement; `launch/` does not
independently enforce depth.

## Spawn Wait — Checkpoint vs Hard Timeout

`spawn_wait_sync()` has two timeout modes:

**Checkpoint mode** (default, `timeout_explicit=False`): waits up to `checkpoint_seconds` (harness-
aware yield interval), then returns a `SpawnWaitMultiOutput` with `checkpoint=True` and
`checkpoint_pending_ids`. The caller must re-run `spawn wait` to continue. This is the LLM-agent
pattern — agents get a periodic yield to check progress rather than blocking indefinitely.

**Hard timeout** (`timeout_explicit=True`): waits up to `timeout` minutes, then raises `TimeoutError`
with an actionable message listing pending spawn IDs and suggested next commands.

The yield interval is resolved from `MERIDIAN_HARNESS` env var (parent harness's prompt-cache TTL
awareness) unless `yield_after_secs` is explicitly passed.

## No-Arg Wait Scoping

When `spawn wait` is called with no explicit IDs (from inside a spawn):
- Discovers active spawns for the current `MERIDIAN_CHAT_ID`
- **When called from a nested spawn**, scopes to descendants only — not siblings, ancestors, or the
  primary session. This prevents a nested spawn's `wait` from blocking on the entire chat tree.
- If `MERIDIAN_CHAT_ID` is absent, raises `ValueError` (not in a session)

## Cancel-All Identity Model

`SpawnCancelAllInput` does not carry a `chat_id` field. Caller identity is derived via
`runtime_context(ctx)` inside `spawn_cancel_all_sync()` — the same pattern `spawn_wait_sync()`
uses. The CLI passes `ctx=RuntimeContext.from_environment()` rather than reading
`os.getenv("MERIDIAN_CHAT_ID")` directly.

This matters: env reading in op API inputs leaks CLI-layer concerns into the service layer.
If you add a new cancel or wait operation, derive identity from `ctx`, not from input fields.

**Subtree scoping from nested spawns:** When `MERIDIAN_SPAWN_ID` is set (the caller is itself a
spawn), `spawn_cancel_all_sync()` uses `state.spawn_tree.descendant_id_set()` to restrict cancellation to that
spawn's subtree. Siblings, ancestors, and the primary session are not touched. When called from
a root/primary context (no `MERIDIAN_SPAWN_ID`), the full chat scope applies.

`--include-others` bypasses the subtree scope and cancels across all non-primary running spawns.
`--include-primaries` is separately required to cancel primary sessions.

**Early-exit guard:** `spawn_cancel_all_sync()` exits immediately with no side effects when
`caller_chat_id is None and caller_spawn_id is None and not payload.include_others`. Both
identity fields must be absent — a nested spawn that has a `spawn_id` but no `chat_id` should
not be early-exited; it can still scope cancel-all to its descendants. The previous guard only
checked `caller_chat_id is None`, which incorrectly short-circuited nested spawns without a
chat_id.

**`_row_in_cancel_scope()` helper:** Extracted from the inline boolean in the list comprehension.
Encapsulates the three-branch cancel scope logic:
- `include_others=True` → always in scope
- `descendant_ids` set (nested spawn caller) → only if row.id is in the subtree
- `descendant_ids=None` (root/primary caller) → only if row.chat_id matches caller_chat_id

## session_config_dir Wire Rename

`SpawnDetailOutput` exposes the field as `session_config_dir` (wire key: `session_config_dir`).
The underlying storage field on `SpawnRecord` is still `claude_config_dir` — the rename is
presentation-only. `detail_from_row()` in `query.py` maps `row.claude_config_dir →
session_config_dir`. Do not rename `SpawnRecord.claude_config_dir`; that touches persisted
per-spawn `state.json` records and would require migration.

## Execute Module Split

`execute.py` is now a re-exporter. The implementation lives in four focused modules:

- `execute_init.py` — spawn record initialization, pre-run setup (`_init_spawn`,
  `depth_exceeded_output`, `depth_limits`, env helpers).
- `execute_bg.py` — background execution: `BackgroundWorkerLaunchRequest` persistence,
  `_build_background_worker_command`, `_background_worker_main`, `_cleanup_background_runtime_artifacts`.
- `execute_runner.py` — blocking execution path, `launch_prepared_spawn()`.
- `execute_session.py` — session management during execution.

`execute.py` re-exports the public surface (`execute_spawn_blocking`, `execute_spawn_background`)
and the depth guard helpers. Import from `execute.py` only — do not reach into the `execute_*`
modules directly from outside `ops/spawn/`.

## Fork / Continue Cross-Harness Guard

`spawn_fork_sync()` and `spawn_continue_sync()` both resolve the effective target harness via a
dry-run `_resolve_effective_fork_target_harness()` call before spawning. If the source and target
harnesses differ, the operation raises `ValueError` — cross-harness continuation is not supported.

`spawn_fork_sync()` copies `goal` from the source spawn row when no `--goal` is provided.

`spawn_continue_sync()` does not assemble replay policy. It resolves the source spawn/session,
rejects launch-identity, policy, work, and task-dir mutations
(`api.py:_reject_continue_policy_overrides`), then delegates exact-continue normalization to
`launch/continue_replay.py` (`ContinueReplayContract`). Agent opt-out is a launch-identity
mutation, not a harmless absence. Contract depth:
[launch/.context/CONTEXT.md#exact-continue-replay](../../../launch/.context/CONTEXT.md#exact-continue-replay).

## prepare.py — The One REQUIRED Path

`prepare.py` uses `LaunchArgvIntent.REQUIRED` on `LaunchCompositionSurface.SPAWN_PREPARE`. This is
the **only** execution path that builds a real argv — used for `--dry-run` display to populate
`cli_command` in `SpawnActionOutput`. All actual execution paths use `SPEC_ONLY`. Do not add
`REQUIRED` elsewhere.

## Compose-once handoff (foreground + streaming-serve)

`prepare.py` returns `SpawnCreateArtifacts(request, prepared)` after `compose_spawn_launch_surface`.
Blocking CLI execute passes `prepared` into `launch_prepared_spawn` for **bind-only** when session/fork
did not change policy inputs (`_spawn_request_needs_recompose`). Full re-compose only on session/fork
mutation. Dry-run create uses `dry_run=True` on compose; real spawns use `dry_run=False` (not CLI
`--dry-run` alone).

`SpawnApplicationService.prepare_spawn()` composes once for streaming-serve,
reserves `spawn_id`, then binds with final `MERIDIAN_SPAWN_ID` overrides.
`streaming_serve` reuses `PreparedSpawn.launch_context` — no third compose.

## Background Spawn Trust Model (Phase 3A)

Parent create still composes once (`build_create_payload`). The detached worker loads persisted
`BackgroundWorkerLaunchRequest` (`SpawnRequest` + `LaunchRuntime`) and composes once at worker launch
(one Mars call in the worker; no persisted `PreparedLaunchSurface` yet). Persisted `SpawnRequest.model`
stays canonical CLI token; harness argv uses Mars `harness_model` at bind. Phase 3B (persisted prepared
handoff for worker bind-only) is deferred unless worker startup remains too slow.

## SpawnActionOutput Wire Shape

`to_wire()` is for explicit `--format json` consumers (agents, scripts). `to_agent_wire()` is the
sparse shape used when agents run without an explicit format flag — it omits dry-run detail and
adds the `wait_required` / `terminal: false` fields for background spawns.

The distinction matters: agents that parse `to_wire()` without requesting it explicitly will
receive `to_agent_wire()` instead.

## SpawnActionOutput Text Format Contract

`format_text()` branches on `FormatContext.verbosity` and spawn status:

- **`verbosity == 0` + terminal status** (`succeeded`, `failed`, `cancelled`) + non-background:
  uses `_format_terminal_text()`. Output: `{spawn_id} {status} ({duration}s)\n\n{report body or
  "(no report)"}\n\nTranscript: meridian session log {spawn_id}`. Warning and error fields appear
  before the report body.
- **All other cases** (non-terminal, background, `--verbose`): uses the existing verbose format
  (`_format_default_text()` / `_format_verbose_text()`).

## SpawnDetailOutput format_text / format_wait_text

`format_text()` now accepts `FormatContext`:
- `verbosity == 0` → `_format_moderate_text()`: labeled fields (`Spawn`, `Status`, `Model`,
  `Duration`, `Work`, `Report`) plus report body when included.
- `verbosity > 0` → `_format_verbose_text()`: full kv_block with internal status diagnostics
  (for example primary activity/backend metadata and Pi cleanup fields).

`SpawnWaitMultiOutput.format_text()` delegates to `format_wait_text()` for single-spawn waits,
which uses verbosity-gated compact vs verbose.

`meridian.spawn.status` now has a manifest-level default of `include_report_body=False`
(`SpawnStatusInput`), while `meridian.spawn.show` keeps `include_report_body=True`
(`SpawnShowInput`). CLI defaults were already aligned (`status` report-off, `show` report-on).

## Foreground Path Populates report

`execute_spawn_blocking()` in `execute.py` reads `report.md` after spawn completion and
passes `report=report_body` into `SpawnActionOutput`. Previously `report` was always `None` for
foreground results. Text output uses that report for compact rendering; explicit JSON also exposes
the primary result content structurally with report and transcript fields.

Callers in default text mode now get the report inline without needing a separate `spawn show`
call. Callers that need machine-readable output should request `--format json`.

## Lateral Links

→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — ops/ layer overview, SpawnApplicationService, SEAM-1/2/3
→ [../../../launch/.context/CONTEXT.md](../../../launch/.context/CONTEXT.md) — launch mechanism ops/spawn drives

## Pi Nested Stale Detection

`_read_only_nested_staleness_view()` in `query.py` handles the case where a nested
spawn reads the status of a Pi spawn. Pi spawned sessions stay alive after task
completion (quiescence model), so a nested reader observing a non-terminal Pi spawn
must detect staleness differently than for other harnesses.

### Detection Method

The staleness check uses file modification times rather than PID liveness:
- Reads mtimes of five files: `heartbeat`, `history.jsonl`, `output.jsonl`,
  `meridian_lifecycle_events.jsonl`, `stderr.log`, `report.md`
- If ANY file has been modified within the last 120 seconds
  (`_NESTED_READ_HEARTBEAT_WINDOW_SECS`), the spawn is considered alive — the
  Pi process may be blocked on lifecycle processing
- A 15-second startup grace period (`_NESTED_READ_STARTUP_GRACE_SECS`) from
  `started_at` prevents false staleness detection during Pi initialization
- A 5-second post-runner-exit grace period
  (`_NESTED_READ_POST_RUNNER_EXIT_FINALIZATION_GRACE_SECS`) allows the runner
  process to finalize before the spawn row is considered stale

If no file has been updated recently, the spawn is marked as `failed` with
`error="stale_nested_read"` or `error="stale_nested_read_no_pid"`. This is a
**read-only view** — `_read_only_nested_staleness_view()` returns a copy of the
record with modified status, not a mutation.

### Why File-Based Instead of PID-Based

`is_process_alive()` (PID check) doesn't work for Pi spawned sessions because:
1. The Pi process may be alive but blocked (quiescence drain waiting for children)
2. The Pi process may have exited but the spawn row hasn't yet been reconciled
   (cleanup phases still running)

File mtime checks catch both cases: a blocked process usually writes heartbeat
timestamps, and a dead process stops updating all files.

### Scope: Read-Only Nested Context Only

This detection only applies when `is_root_side_effect_process()` is false — i.e.,
a nested agent reading another spawn's status. The root Meridian process uses the
reaper (`lib/state/reaper.py`) for staleness detection, which has its own
activity/grace windows and does actual row mutations.

### Pi Cleanup Telemetry in `spawn show`

`_pi_cleanup_telemetry()` reads `history.jsonl` for `meridian.pi.lifecycle.phase`
events with phases starting with `cleanup_`. It extracts cleanup status
(`running → completed → escalated → failed`), reason, and error to display in
`meridian spawn show` output. The highest-severity cleanup status across all phase
events is reported.

## Related .context/

- [../../../streaming/.context/CONTEXT.md](../../../streaming/.context/CONTEXT.md) — Pi quiescence drain, child wave timeouts, micro-drain
- [../../../harness/.context/CONTEXT.md](../../../harness/.context/CONTEXT.md) — PiAdapter, lifecycle events, runtime resolution
- [../../../harness/connections/.context/CONTEXT.md](../../../harness/connections/.context/CONTEXT.md) — PiRpcConnection dual-event-source
