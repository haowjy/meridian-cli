# ops/spawn — Contracts and Architecture

## Spawn Creation Boundary

`spawn_create_sync()`, continue, and fork share one preparation path. It inspects
authority and checks the launcher-owned caller boundary before write bootstrap. With
a boundary, it composes the resolved launch policy read-only and enforces the floor.
Every constrained caller records the effective network its harness applies. The child check
pure-binds a preview before any state write, then uses the bound child environment with the
shared network-facts builder and harness projection port. It refuses a value not contained
by the caller: an offline child is always contained; otherwise finite allows must narrow and
denies must grow. The report uses the same pure projection. A
boundary written before the network field existed has no network floor.
Every real launch calls `prepare_for_runtime_write()` afterward through one deferred
materialization path. Dry-run binds its preview without materializing. Continue
performs its state sweep after preparation succeeds; fork reuses the prepared artifacts
for its cross-harness guard.

The caller boundary rejects a no-capability caller and explicit harness-native
passthrough arguments before source-state reads. Continue and fork can discover
additional passthrough arguments in the resolved source launch snapshot; they run
the same `CallerBoundaryCheck` again immediately after source resolution and before
model or policy composition. Passthrough is all-or-nothing under a floor because
each harness owns native flags that can supersede Meridian's resolved sandbox or
approval arguments.

This ordering is security-sensitive: the standalone harness escape runs `meridian`
outside the native sandbox, so policy refusal must precede identity creation, state
locks, telemetry files, and spawn rows.

## Depth Guard

`spawn_create_sync()` checks `max_depth_reached()` **before** executing any spawn. When depth
is exceeded, it returns `depth_exceeded_output()` with no side effects — no spawn row is created.

Depth is read from `_MERIDIAN_DEPTH` env var in the child. The reaper also checks `_MERIDIAN_DEPTH`
and skips reaping when inside a spawn. This is an ops-layer enforcement; `launch/` does not
independently enforce depth.

## Spawn Wait — Checkpoint, Hard Timeout, and Fail-Fast

`spawn_wait_sync()` has three exit paths:

**Checkpoint mode** (default, `timeout_explicit=False`): waits up to `checkpoint_seconds` (harness-
aware yield interval), then returns a `SpawnWaitMultiOutput` with `checkpoint=True` and
`checkpoint_pending_ids`. The caller must re-run `spawn wait` to continue. This is the LLM-agent
pattern — agents get a periodic yield to check progress rather than blocking indefinitely.

**Hard timeout** (`timeout_explicit=True`): waits up to `timeout` minutes, then raises `TimeoutError`
with an actionable message listing pending spawn IDs and suggested next commands.

**Fail-fast** (`fail_fast=True`): after each poll, if any completed spawn has status `failed` or
`timed_out` AND spawns are still pending, returns early with `fail_fast=True` and `pending_ids`
in the output. `cancelled` does not trigger fail-fast. Snapshot consistency: each poll pass keeps
the latest row of every waited spawn, and every exit builds its result and pending set from that
one pass, so each spawn appears in exactly one category.

With `observe` (the default), the wait leases its pending launches in the caller's
`spawns/<caller>/observed-launches.json` while it polls. A caller whose spawn row is gone writes
nothing. Every exit, including errors and the hard timeout, makes one write in `finally` that
releases the lease and records the terminal launches it returned as consumed.

**A wait is on a launch.** Each explicit target is waited on as launch `n`, pinned when the
wait resolves it and checked every poll through `poll_launch(runtime_root, id, n)`: once the
spawn has moved past `n` (a continue), the answer is `launches/<n>/launch.json`, so the wait
returns the launch it started on. A re-run after `--yield-after-secs` is a new process, so
an explicit wait by a caller with a spawn ID stores its pin in that caller's
`observed-launches.json` (`wait_pins`) and the re-run honours it while the pin is unexpired
(refreshed each poll to now plus the yield interval), its launch is unconsumed (union reader,
any path), and the caller has not continued the target (`clear_wait_pin`, for the continue's
commit). Otherwise the wait pins the current launch. A caller with no spawn ID, and no-arg
waits, pin in-process only.

The yield interval is resolved from `_MERIDIAN_HARNESS` env var (parent harness's prompt-cache TTL
awareness) unless `yield_after_secs` is explicitly passed.

## No-Arg Wait Scoping

When `spawn wait` is called with no explicit IDs (from inside a spawn):
- Discovers active spawns in the transitive subtrees of `owned_parent_ids` (the roots owed
  results use) in one recursive index query: every primary generation of the chat for a primary,
  so background work started before a continue is waited on; the caller's own subtree for a
  nested caller. Peers that only share the chat, siblings, ancestors, and the primary are
  excluded. A call without a spawn ID fails.
- Adds terminal results still owed to the caller. A primary uses `owed.py`'s collection scope:
  background direct children of every primary generation for `MERIDIAN_CHAT_ID`, minus
  consumption and live leases read across those generations. A nested caller owns only its own
  background direct children, so its parent scope contains only itself. Consumption still uses
  the shared current/legacy observations (including terminal results shown by cancel, show, or
  report-show surfaces), Pi receipts, and clears; leases come from observations.
  Foreground, archived, retired, historical, and quarantined rows are not owed wait targets.
- Takes at most the oldest 32 rows already terminal when target resolution runs, using the same
  `_DELIVERY_MAX_MEMBERS` bound as collection. Later owed rows remain eligible for the next wait
  or collection. Active targets are not capped.
- Keeps owed-finished rows out of polling: `owed.py` resolves them once, removes IDs already in
  the active set, and caps the remainder. `models.py:render_delivery` renders their details
  (status/outcome, worker, failure, distinct task directory, and show lookup) without
  `report_body`, including JSON `--full`. JSON places them in
  `owed_spawns`; `spawns`, run counts, fail-fast, single-result compatibility fields, and the exit
  code describe only rows active at resolution. Those blocked-on rows keep normal wait rendering
  and their requested report body after the wait blocks for them.
- An observing wait leases the combined set, then polls only the active IDs. Its existing
  `finally` releases the lease and consumes terminal blocked-on details plus every resolved owed
  ID. Collect can still win the pre-lease race and cause one duplicate header; this accepted cost
  avoids a second snapshot/recheck mechanism.
- `--no-observe` still includes owed terminal results because it means fetch without consuming; it
  creates no lease or observation, so the same header can appear again until a normal wait or ack.
- On every complete, fail-fast, or checkpoint return after successful initial discovery, runs the
  same target discovery once more and appends children that appeared after target resolution under
  `Since this wait started:`. An initial discovery-pending checkpoint has no baseline and skips the
  trailer. Active children outside the original blocked-on set render as `running`. Newly owed
  terminal children outside the original blocked-on and owed sets render as `finished` for success
  or retain the failure terminal word (`failed`, `timed_out`, or `cancelled`). "Appeared" is set
  membership, not time: the initial discovery keeps every finished owed id it saw, including rows the
  32-member cap held back, and the return discovery excludes those before applying the cap. Crashed
  children with no recorded finish time are therefore still listed. Structured output uses
  `since_started_spawns`.
- The two halves keep the start-of-wait scopes: `running` comes from the descendant subtree (so a
  grandchild can appear), `finished` from the owed scope (background direct children only). A
  grandchild listed as running at one return is not listed once it finishes; that is the owed
  scope, not a gap to fix here.
- The return-time discovery is advisory and read-only: its rows are not waited on, leased, consumed,
  counted, or included in fail-fast and exit decisions. It runs once at return, never per poll. Any
  state or index failure (busy or incomplete index, OS error, quarantined row) omits the section:
  the wait's `finally` consumes its results regardless, so failing the wait here would lose them. Explicit-ID waits do not
  run it. `--no-observe` keeps the same trailer.
- If `MERIDIAN_CHAT_ID` is absent, raises `ValueError` (not in a session)

## Idle Spawn Delivery Contract

`owed.py` owns the owed-child definition shared by no-arg wait and the hidden Claude runtime
protocol:

- `spawn collect --exclude p1,p2` returns exactly one JSON object with
  `{kind: "delivery"|"empty", ids: [...], text: "..."}` and exit 0. `ids` are bare,
  de-duplicated spawn IDs (launches are internal), which `--exclude` and `spawn ack` take
  back; `--exclude pN` excludes every launch of `pN`. It never consumes, but a delivery
  records the launches it delivered (`delivered: {pN: [K, ...]}`) in the caller's
  `observed-launches.json`, one write per delivery, so `ack` can consume exactly those.
  Index discovery is parent-scoped; polling re-reads only still-owed authority rows with
  derived status.
- Owed membership (`state/spawn_ownership.owed_launches`, shared with the Pi drain) is background direct children of all primary generations for
  `MERIDIAN_CHAT_ID`, plus the background launches a continue moved past while still owed
  to one of those parents (`owed_launches`, projected into the index's side table; their
  rows are `launches/<n>/launch.json`, whoever the spawn belongs to now), minus consumption read across those same generations and
  minus `--exclude`. Consumption includes CLI wait/ack observations, cancelled results,
  and terminal report bodies shown by the CLI, plus Pi's delivery receipts and cleared
  IDs. One Pi-private-state reader unions the current
  and legacy observations, receipts, and cleared IDs per parent. It skips a damaged
  source with a diagnostic, accepting a possible repeated header instead of failing
  wait or retrying collection forever. Birth-verified explicit-wait leases skip a
  child without letting the collector exit early.
- An initially ready result returns the ready terminal subset. Otherwise the fixed
  owed set settles together. A final consumption/lease check precedes return.
- A delivery admits at most 32 results. Each member is header-only: identity and
  outcome, worker, failure and distinct task directory when present, then a
  `meridian spawn show <id>` lookup. The collector does not read report bodies;
  Claude leads explicitly read one with `spawn show`. The exception is a launch the spawn
  has since continued past: `spawn show` and `report show` describe the next launch, so that
  member carries its own report inline (`delivery_detail`; 64 KiB, the Pi path's bound),
  and so does the owed list of a no-argument `spawn wait`. Printing a terminal report body
  through `spawn show` or `spawn report show` consumes it, as do `spawn wait`
  completion, `spawn ack`, and a `spawn cancel` that reports `cancelled`.
- `spawn ack <ids...>` consumes, under the caller's current primary, exactly the launches
  the last delivery recorded for each ID and drops the record. With no record (a person
  running `ack`) it consumes the ID's finished, owed, unconsumed launches, never one still
  running. It returns exactly `{acked: [...]}` and fails when that primary's row is gone. The CLI
  refuses `spawn ack -C` before calling the operation. Reading across the chat is what
  makes an ack written after resume consume a child of an earlier primary.

Both commands use `observed-launches.json`, the same state as explicit `spawn wait`.
The collector retries index contention and malformed owned authority internally,
and exits cleanly when its parent PID changes.

`owed.py:record_result_shown_to_caller()` is the one best-effort write path for
operations that put a terminal result in front of their caller. It records under the caller,
like wait and ack. Only scopes that read that caller's observations consume it, so a nested
non-owner's read or cancel does not consume the primary's child. Calls without a caller
spawn, active results, and empty report bodies are not recorded. `consume_result_if_shown()`
owns those shared show/status/report-show conditions. Show and status record only when
`observe=True` and their selected output includes a report body; report-show records a
non-empty terminal report in every format.
Cancel records only an outcome reported as `cancelled`; `finalizing` and already-completed
non-cancel outcomes stay owed. Show, status, report-show, cancel, and cancel-all operations
default `observe=False` so internal readers remain pure; their CLI surfaces opt in and expose
`--no-observe`, except cancel-all, whose CLI always opts in. The shared CLI observer helper
withholds the caller spawn ID under `-C`, so commands directed at another project do not
record (and ack is refused). An observation failure never replaces the result already shown: lock
contention after about one second is a warning, read-only-filesystem errors are debug-only.

## Cancel-All Identity Model

`SpawnCancelAllInput` does not carry a `chat_id` field. Caller identity is derived via
`runtime_context(ctx)` inside `spawn_cancel_all_sync()` — the same pattern `spawn_wait_sync()`
uses. The CLI's shared observer helper passes the ambient chat ID for scoping plus an
observer spawn ID only when allowed. This preserves normal no-arg wait and cancel-all
scoping while making `-C` non-observing.

This matters: env reading in op API inputs leaks CLI-layer concerns into the service layer.
If you add a new cancel or wait operation, derive identity from `ctx`, not from input fields.

**Subtree scoping from nested spawns:** When `MERIDIAN_SPAWN_ID` is set (the caller is itself a
spawn), `spawn_cancel_all_sync()` uses `state.spawn_tree.descendant_id_set()` to restrict cancellation to that
spawn's subtree. Siblings, ancestors, and the primary session are not touched. When called from
a root/primary context (no `MERIDIAN_SPAWN_ID`), the full chat scope applies.

Cancel-all selects stored-active rows, not derived-running rows. Its cancel pipeline
claims and finalizes a dead lock-backed target under the launch lock.

`--include-others` bypasses the subtree scope and cancels across all non-primary stored-active spawns.
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
- `descendant_ids=None` (root/primary caller) → only if the row's owner chat matches caller_chat_id

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
(`SpawnShowInput`). Both operation inputs default `observe=False`; CLI defaults opt in, but
status can consume only when `--full` actually includes a non-empty terminal report.

## Foreground Path Populates report

`execute_spawn_blocking()` in `execute.py` reads `report.md` after spawn completion and
passes `report=report_body` into `SpawnActionOutput`. Previously `report` was always `None` for
foreground results. Text output uses that report for compact rendering; explicit JSON also exposes
the primary result content structurally with report and transcript fields.

Callers in default text mode now get the report inline without needing a separate `spawn show`
call. Callers that need machine-readable output should request `--format json`.

## Caller default and continuation details

- `spawn done`, `spawn rearm`, and `spawn children` default to the caller's own spawn:
  `_resolve_caller_spawn_id()` takes the explicit value, then `runtime_context(ctx).spawn_id`,
  else raises "Spawn ID is required. Pass spawn_id or set MERIDIAN_SPAWN_ID." `spawn stats`
  without an id keeps its older meaning (every spawn); `spawn message` stays explicit.
- Native-ID continuation resolves the harness namespace before reading the session's
  recorded spawn ID, checking that row's exact chat and harness. Never substitute an
  owner's newest spawn; keep the supplied native ID even when the chat has a newer
  identity. `--harness` disambiguates ownership; it cannot reconstruct missing retained
  spawn metadata.
- Exact continue passes its verified source chat into the spawn session scope; fresh and
  fork launches allocate separate chats. The captured `SessionAttempt` follows the session
  generation into streaming execution, and native-ID callbacks and accepted-running
  model-selection writes use that capture. A recording failure is a coordination error,
  not a reason to replay the model turn.
- A spawn's `--from` stays in `context_refs` and is not copied onto
  `SessionRequest.context_from_*`: lineage covers primary sessions only, so the prior
  context reaches the child only as rendered context blocks. `prepare.py` calls
  `context_ref.resolve_context_ref()` once per spawn; launch only renders the result.
- Stored vs derived status: `read_derived()` / `overlay_derived()` serve liveness, display,
  wait, and idle. Cancel convergence, archive, and destruction select stored status;
  cancel targets stay stored-active until `finalize_dead_launch()` persists the outcome,
  so overlay-first selection would skip dead launches that still need convergence.

## Lateral Links

→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — ops/ layer overview, SpawnApplicationService, SEAM-1/2/3
→ [../../../launch/.context/CONTEXT.md](../../../launch/.context/CONTEXT.md) — launch mechanism ops/spawn drives

### Pi Cleanup Telemetry in `spawn show`

`_pi_cleanup_telemetry()` reads the `spawns/<id>/pi-lifecycle.json` sidecar through
`state/pi_lifecycle.read()` — not `history.jsonl`. It extracts cleanup status
(`running → completed → escalated → failed`), reason, and error to display in
`meridian spawn show` output. The highest-severity cleanup status recorded in the
sidecar is reported.

## Related .context/

- [../../../streaming/.context/CONTEXT.md](../../../streaming/.context/CONTEXT.md) — Pi quiescence drain, child wave timeouts, micro-drain
- [../../../harness/.context/CONTEXT.md](../../../harness/.context/CONTEXT.md) — PiAdapter, lifecycle events, runtime resolution
- [../../../harness/connections/.context/CONTEXT.md](../../../harness/connections/.context/CONTEXT.md) — PiRpcConnection dual-event-source
