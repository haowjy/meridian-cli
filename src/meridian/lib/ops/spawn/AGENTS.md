# ops/spawn/ — Spawn Policy Layer

The policy half of spawn creation and execution. Surfaces `spawn_create`, `spawn_list`,
`spawn_cancel`, `spawn_wait`, `spawn_fork`, `spawn_continue` to CLI and MCP.
Does not compose argv or env — that's `launch/`. Owns: depth checks, work attachment,
input validation, and routing to foreground vs background execution.

## Execution Paths

Both paths converge at `launch_prepared_spawn()`:

**Foreground:** `execute_spawn_blocking()` → creates spawn row → calls
`launch_prepared_spawn()` via `asyncio.run()` → `execute_with_streaming()`.

**Background:** `execute_spawn_background()` → creates spawn row → persists
`BackgroundWorkerLaunchRequest` to disk → detaches subprocess. Worker calls
`_execute_existing_spawn()` → `launch_prepared_spawn()`.

`launch_prepared_spawn()` owns pre-run failure finalization. If it throws, it writes
`launch_failure` before re-raising. This is safe because `complete_spawn()` is idempotent.

## Depth Guard

`api.py` checks `max_depth_reached()` before every spawn execution. A spawn chain
that exceeds the depth limit returns `depth_exceeded_output()` instead of creating
a new spawn. `_MERIDIAN_DEPTH` is an env var that increments with each spawn level;
nested processes also read it to skip reaping side effects.

## Wait Behavior

`spawn_wait` has two modes:
- With spawn IDs: waits for specific spawns.
- Without args (no-arg form): a primary waits for active descendants plus finished
  background children still owed across its chat's primary generations; a nested spawn
  waits for its active transitive subtree plus its own owed direct background children.

No-arg candidate membership is scoped in the history index: owner-filtered SQL for a
primary, recursive subtree SQL for a nested spawn. Discovery then re-reads only those
selected `state.json` files with derived status before answering liveness. Recursive traversal keeps
archived ancestors as edges so active descendants beneath them remain visible.
`owed.py` separately owns the shared wait/collect definition of finished unconsumed,
unleased background children, including Pi delivery receipts and explicit dismissals as
consumption. No-arg wait resolves at most the oldest 32 already-finished members once,
minus the active target IDs, leases them without putting them through the poll loop, and
renders their details with `models.py`'s shared header-only renderer. Active targets remain
uncapped and keep report output. Owed rows are separate from `spawns` in structured output
and do not affect fail-fast, waited-run aggregates, or the command's exit status. The
existing `finally` consumes the owed IDs beside terminal active details.
`--no-observe` includes owed children with the same rendering but neither leases nor
consumes them, so they remain eligible for a later normal wait or delivery.
Every no-arg return after successful initial discovery runs that same discovery once
more and appends children that appeared while the wait ran. An initial
discovery-pending checkpoint has no baseline and skips the trailer. This
`since_started_spawns` trailer is advisory: it is not leased, consumed, counted, or
included in fail-fast and exit decisions.

`--fail-fast` exits as soon as any spawn reaches `failed` or `timed_out`, reporting
which spawns failed and which are still pending. `cancelled` does not trigger
fail-fast (cancellation keeps existing aggregate-exit behavior).

Checkpoint polling respects the depth-appropriate yield interval (read from
`_MERIDIAN_HARNESS` env — the calling orchestrator's own checkpoint interval).
No-arg discovery always lets an absent history index finish its separate, sticky
cold-build budget first. Its deadline then bounds one catch-up attempt; if the index
is still busy, wait yields an explicit discovery-pending checkpoint instead of
claiming the session has no pending spawns.

Result fetchers that do not consume a parent's completion notification must use
`SpawnWaitInput(observe=False)` (`spawn wait --no-observe`). Normal waits observe
by default; formatting a notification is not delivery.

Hidden `spawn collect` is the idle-runtime counterpart, not another wait mode. It
discovers background direct children of every primary generation in the caller's
chat, subtracts durable observations before authority reads, skips live explicit-wait
leases, and consumes nothing. If a result was already terminal it returns the terminal
subset immediately; otherwise it waits for the owed set to finish. Hidden `spawn ack`
records the delivered IDs only after the harness admits their message.

## Stored vs Derived Status

Use `read_derived()` / `overlay_derived()` for liveness, display, wait, and idle.
Selection for cancel convergence, archive, and destruction uses stored status;
cancel targets remain stored-active until `finalize_dead_launch()` persists the
outcome. Overlay-first selection skips dead launches that still need convergence.

## The Exception to SPEC_ONLY

`prepare.py` uses `LaunchArgvIntent.REQUIRED`. This is the only execution path
that needs real argv — it populates `cli_command` for dry-run display. All actual
execution paths use `SPEC_ONLY`. Do not set `REQUIRED` on execution paths.

## Fork/Continue Cross-Harness Guard

Fork and continue operations check that the target spawn's harness matches the
current harness context. Cross-harness fork/continue is not supported — the guard
raises before any state is created.

`spawn_continue` does not assemble replay policy itself. It resolves the source
spawn/session, allows explicit model overrides but rejects other launch-identity,
policy, work, and task-dir mutations, then
delegates exact-continue normalization to `launch/continue_replay.py`'s
`ContinueReplayContract`. Exact continue passes its verified source chat into the
spawn session scope; fresh and fork launches allocate separate chats. Agent opt-out is treated as a launch-identity mutation,
not as a harmless absence.

Native-ID continuation resolves the harness namespace before reading the session's
recorded spawn ID, checking that row's exact chat and harness. Never use an owner's
newest spawn as a substitute. Keep the supplied native ID, even when the chat has a
newer identity. Missing retained spawn metadata cannot be reconstructed by
`--harness`; that flag disambiguates ownership, not missing history.

The captured `SessionAttempt` follows the session generation into streaming
execution. Native-ID callbacks and accepted-running model-selection writes use
that same capture. Recording failure is a coordination error, not a reason to
replay the model turn.

## Key Entry Points

- `api.py` — all spawn operations (sync and async variants).
- `models.py` — `SpawnCreateInput`, `SpawnDetailOutput`, `SpawnWaitInput`, etc.
- `execute.py` — re-exporter for public entry points (`execute_spawn_blocking`,
  `execute_spawn_background`) and depth guard helpers. Implementation split across:
  `execute_init.py` (spawn record init), `execute_bg.py` (background worker),
  `execute_runner.py` (blocking path), `execute_session.py` (session management).
  Import from `execute.py` only — do not import `execute_*` modules from outside `ops/spawn/`.
- `prepare.py` — `build_create_payload()` → `SpawnCreateArtifacts` (compose once; dry-run bind for `cli_command`).
- `query.py` — `resolve_spawn_reference()` and spawn-record reads.
- `context_ref.py` — `resolve_context_ref()` turns a `--from` ref (spawn or chat) into a
  `ContextRef`. `prepare.py` calls it once per spawn; launch only renders the result.
  A spawn's `--from` stays in `context_refs` and is not copied onto
  `SessionRequest.context_from_*`: lineage covers primary sessions only, so no edge
  is persisted for a spawn and its prior context reaches the child only as rendered
  context blocks (see `cli/.context/CONTEXT.md`, session browse).
- `failure_policy.py` — depth guard, depth limit helpers.

## Anti-Patterns

**Don't create spawn rows before launch preparation on new code paths.**
The existing subprocess path does this (known gap); don't replicate the pattern. New
paths should use the resolve-before-persist streaming-serve model.

**Don't set `LaunchArgvIntent.REQUIRED` on execution paths.** Only `prepare.py` gets
a real argv — execution paths use `SPEC_ONLY`.

## Depth

→ [.context/CONTEXT.md](../.context/CONTEXT.md) — wait checkpoint behavior, no-arg
   wait scoping, background worker trust model, SEAM-1/2/3 contracts.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — ops/ policy layer overview;
  SpawnApplicationService layer diagram; execution path ownership.
- [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — launch mechanism
  this drives; [exact continue replay contract](../../launch/.context/CONTEXT.md#exact-continue-replay)
  (`ContinueReplayContract`).
