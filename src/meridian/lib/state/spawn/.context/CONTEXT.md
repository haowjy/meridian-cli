# state/spawn — Context

Detailed contracts for spawn state persistence. The parent `.context/` covers
the dual-root layout and atomic write rules — this file covers the spawn-specific
model split, two-tier write discipline, and finalization policy.

## SpawnRecord vs StoredSpawnState

Both projections share a frozen `SpawnStateFields` base that carries every shared
field once. `SpawnRecord` (in `model.py`) adds the full prompt body
(`prompt: str | None`). `StoredSpawnState` (in `repository.py`) stores only
`prompt_length: int | None` — the prompt body lives in `starting-prompt.md` to
keep `state.json` reads lightweight.

`record_to_stored_state()` / `stored_state_to_record()` are the conversion functions.
`read_state()` calls both automatically; callers don't need the stored form directly.
An import-time field-accounting guard (`_enforce_spawn_state_field_accounting`)
fails if either projection stops accounting for a shared field.

## Discriminated Lifecycle Facts

Lifecycle evidence is nested into typed frozen sub-models, not stored as flat
top-level fields. A status and its accompanying facts form one coherent parse
unit — it is impossible to construct a terminal spawn without complete terminal
facts, or carry terminal facts on an active spawn.

```
SpawnStateFields
  status: PersistedSpawnStatus          # top-level authority
  runner_exit: RunnerExitFacts | None   # runner-resolved terminal intent
  terminal: TerminalFacts | None        # finalized outcome + metrics
```

**Enforced-equivalence invariant.** When `status` is terminal,
`terminal.status` must equal `status` and `terminal` must not be `None`.
When `status` is active or `unknown`, `terminal` must be `None`. A
`model_validator(mode="before")` enforces this at every parse; stale flat
lifecycle fields (`runner_exit_code`, `finished_at`, `exit_code`,
`terminal_origin`, etc.) are rejected outright so historical data is never
silently misinterpreted as the new schema.

**Revalidation on copy.** Both `RunnerExitFacts`, `TerminalFacts`, and
`SpawnStateFields` extend `_RevalidatedFrozenModel`, whose `model_copy(update=)`
round-trips through `model_validate` instead of Pydantic's default shallow copy.
This closes the escape hatch where `model_copy(update={"status": "succeeded"})`
would bypass the discriminant invariant.

Backward-compatible property accessors (`runner_exit_status`,
`runner_exit_code`, `finished_at`, `exit_code`, `terminal_origin`, etc.)
delegate to the sub-models so existing callers compile unchanged.

## Quarantine

Out-of-vocabulary persisted rows are quarantined, never silently omitted or
coerced.

`StoredSpawnState` validates vocabulary fields (`status`, `kind`,
`launch_mode`, and nested `runner_exit.status`, `terminal.status`,
`terminal.origin`) before Pydantic field parsing via a `model_validator`.
Non-string values and unknown enum members both route to the quarantine seam:
type-check runs BEFORE string operations so a non-string value raises
`ValueError` (quarantine) rather than `AttributeError` (crash past the seam).

Single-row reads (`get_spawn`) raise `SpawnStateQuarantined` with a structured
`SpawnStateQuarantineReport`. Collection reads (`list_spawns`) partition valid
rows from quarantine reports into a `SpawnCollection` — callers iterate
the collection for valid rows and inspect `.quarantines` for problem reports.

Migration and telemetry-retention fail closed on quarantine: an unreadable row
may contain active work, so those paths skip it rather than deleting live data.

## Locked Mutation Model

Every published-spawn mutation calls `write_state_locked()`. It acquires the stable
per-spawn lock at `locks/spawns/<id>.lock` (outside the spawn artifact directory,
never unlinked), re-reads `state.json`, applies a pure mutator, and writes
atomically. There is no public unlocked write path — the prior two-tier model
(owner writes without lock / external writes with lock) was collapsed in PR #422.

The conformance guard `tests/contract/test_state_write_conformance.py` rejects new
raw writes to authoritative state files at CI.

## Pure Transitions

`transitions.py` functions (`apply_mark_running`, `apply_record_exited`,
`apply_mark_finalizing`, `apply_finalize`) contain no I/O. Each takes a `SpawnRecord`
and returns a new `SpawnRecord` with updated fields. Callers decide whether to run
on the owner hot path or inside `write_state_locked`'s mutator lambda, then pass the
same transition function either way.

Status transitions are validated against the allowed state machine in
`core.spawn_lifecycle.validate_transition()`. Pass `validate_status_transition=False`
only when the record may be in `unknown` status (legacy migration paths).

## Attempt vs Runner-Exit vs Terminal Facts

The spawn record carries three distinct categories of exit metadata at different
nesting levels. Confusing them produces wrong terminal-state decisions.

### Attempt-level: `last_attempt_exit_code` / `last_attempt_exited_at`

Flat top-level fields overwritten on every harness-attempt drain. They carry no
spawn-level terminal meaning — a `0` exit code can precede retries or
post-attempt budget failures. Written by `apply_record_exited()` in
`transitions.py`.

### Runner terminal intent: `runner_exit: RunnerExitFacts | None`

Frozen sub-model holding the runner's resolved terminal outcome (`status`,
`exit_code`, `error`, `exited_at`). Written exactly once after all attempts
and post-attempt work are complete, before `mark_finalizing()`.

**Authoritative presence check:** `runner_exit is not None`. The reaper pivots
entirely on this; attempt-level fields carry no terminal weight.

**Write sequence (caller contract):**

1. Resolve `terminal_facts` from run conclusion
2. `record_runner_exit()` — persist `RunnerExitFacts` (atomic write)
3. `complete_execution()` → `mark_finalizing()` → `finalize()`

Crash between 2 and 3: reaper sees `runner_exit` on a `running`/`finalizing`
spawn and uses it. Crash before 2: reaper sees no `runner_exit` and
orphan-fails. Writers must cover all terminal paths.

### Finalized outcome: `terminal: TerminalFacts | None`

Frozen sub-model holding the complete finalized state (`status`, `exit_code`,
`finished_at`, `published_at`, `duration_secs`, token/cost metrics, `error`,
`origin`). Written by `apply_finalize()` in `transitions.py`.

The enforced-equivalence invariant ties `status` (top-level) to
`terminal.status`; constructing a mismatch fails at parse time.

### PID-liveness hardening: `runner_created_at_epoch`

`psutil.Process(runner_pid).create_time()` captured alongside `runner_pid`.
Passed to `is_process_alive()` for robust PID-birth-time verification. Falls
back to `started_at` heuristic when `psutil` raises.

## Terminal Write Authority

`decide_terminal_write(current_status, current_terminal_origin, incoming_origin)`:

| Current state | Incoming origin | Decision |
|---|---|---|
| Not terminal | any | `append` (proceed) |
| Terminal, `reconciler` origin | authoritative origin | `replace` (upgrade) |
| Terminal, any other origin | any | `reject` (skip) |
| `None` (no record) | any | `reject` |

Authoritative origins: `runner`, `launcher`, `launch_failure`, `cancel`.
`reconciler` is a weaker origin — the reaper may write it when no runner-origin
terminal event arrived. A subsequent authoritative write upgrades to the stronger
origin via `replace`.

## Anti-Patterns

**Don't read raw `state.json` directly** — use `read_state()`. Raw reads bypass
Pydantic validation and miss the prompt-body stitch from `starting-prompt.md`.

**Don't call `write_state()` from external processes** — use `write_state_locked()`
with a mutator. Without the lock, external writes race with the runner's hot path.

**Don't skip `validate_status_transition` on production paths** — it guards against
status regressions (e.g. writing `running` after `success`).

**Don't treat `last_attempt_exit_code == 0` as a spawn-success signal.** It is
attempt-level bookkeeping — overwritten on retries and potentially stale after
post-attempt budget checks or guardrail failures that change the outcome. The
runner's terminal intent lives in `runner_exit.status`, not in attempt exit codes.

**Don't trust `durable_report` as success evidence when `runner_exit is None`
and the runner is dead.** A `report.md` from a prior guardrail-failing attempt looks
identical to one from a successful run. The runner is the only entity that knows
whether the spawn succeeded — if it never persisted `runner_exit`, treat the spawn
as an orphan failure even if artifacts exist.

**Don't set flat lifecycle fact fields on the model.** Fields like `exit_code`,
`finished_at`, `terminal_origin` are read-only properties that delegate to the
`terminal` sub-model. Build `RunnerExitFacts` or `TerminalFacts` and pass the
sub-model to the transition function.

**Don't catch `SpawnStateQuarantined` and coerce to a default.** Quarantine
means the row's vocabulary is unrecognizable. Coercing it silently discards the
quarantine signal that migration and retention depend on to fail closed.

## Related

- [`../AGENTS.md`](../AGENTS.md) — entry points
- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — parent state module contracts
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/spawn-finalization.md` — finalization authority lattice, `runner_exit_*` invariant, and reaper contract
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/state-system.md` — per-spawn state layout, locking, and reconciliation model
