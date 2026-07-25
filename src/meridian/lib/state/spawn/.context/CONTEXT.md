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

**Single status authority.** Top-level `status` is the sole status field.
`TerminalFacts` carries terminal evidence (exit code, timestamps, metrics,
origin) but does not repeat status. When `status` is terminal, `terminal`
must not be `None`; when `status` is active or `unknown`, `terminal` must
be `None`. `StoredSpawnState` uses `extra="forbid"`, so unknown fields on a
v3 row are quarantined, never silently accepted.

**Legacy rows (`v: 2` or missing).** `legacy.py` upgrades known pre-v3 shapes
in memory at the parse boundary, BEFORE the strict model validates: flat
`runner_exit_*`/terminal projection fields map to the nested sub-models, an
agreeing nested `terminal.status` is dropped, `RETIRED_LEGACY_FIELDS` (fields
from dead schema generations, e.g. `revision`) are deliberately discarded, and
pre-`published_at` history backfills `published_at := finished_at`. The
upgrader is a mechanical re-shaper, never a reconciler: status conflicts,
partial facts, and truly unknown fields still quarantine. Reads never rewrite
legacy files — the next locked mutation persists v3 naturally. The module is
deletable once pre-v3 rows are gone from the wild.

## Quarantine

Out-of-vocabulary persisted rows are quarantined, never silently omitted or
coerced.

`StoredSpawnState` validates vocabulary fields (`status`, `kind`,
`launch_mode`, and nested `runner_exit.status`,
`terminal.origin`) before Pydantic field parsing via a `model_validator`.
Non-string values and unknown enum members both route to the quarantine seam:
type-check runs BEFORE string operations so a non-string value raises
`ValueError` (quarantine) rather than `AttributeError` (crash past the seam).

Single-row reads (`get_spawn`) raise `SpawnStateQuarantined` with a structured
`SpawnStateQuarantineReport`. Collection reads (`list_spawns`) partition valid
rows from quarantine reports into an immutable `SpawnScan` — callers iterate
`.records` for valid rows and inspect `.quarantines` for problem reports.

Migration and telemetry-retention fail closed on quarantine: an unreadable row
may contain active work, so those paths skip it rather than deleting live data.

## Locked Mutation Model

Every published-spawn mutation calls `write_state_locked()`. It acquires the stable
per-spawn lock at `locks/spawns/<id>.lock`, outside the spawn artifact directory,
re-reads `state.json`, applies a pure mutator, and writes atomically. Lock lifetime
and orphan cleanup follow the parent
[`state/.context/CONTEXT.md`](../../.context/CONTEXT.md#platform-locking); this page does
not restate that contract. There is no public unlocked write path — the prior
two-tier model (owner writes without lock / external writes with lock) was collapsed
in PR #422.

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

Frozen sub-model holding the complete finalized state (`exit_code`,
`finished_at`, `published_at`, `duration_secs`, token/cost metrics, `error`,
`origin`). Written by `apply_finalize()` in `transitions.py`. Does not carry
status; top-level `status` is the sole status authority.

### PID-liveness hardening: `runner_created_at_epoch`

`psutil.Process(runner_pid).create_time()` captured alongside `runner_pid`.
Passed to `is_process_alive()` for robust PID-birth-time verification. Falls
back to `started_at` heuristic when `psutil` raises.

## Terminal Write Authority

Terminal write authority is inlined in `spawn_store.py:finalize_spawn()`. The
finalize mutator checks `current.terminal.origin` against the incoming origin
under the per-spawn lock:

| Current state | Incoming origin | Decision |
|---|---|---|
| Not terminal | any | proceed |
| Terminal, `reconciler` origin | authoritative origin | replace (upgrade) |
| Terminal, any other origin | any | reject (skip) |

Authoritative origins: `runner`, `launcher`, `launch_failure`, `cancel`.
`reconciler` is a weaker origin — the reaper may write it when no runner-origin
terminal event arrived. A subsequent authoritative write upgrades to the stronger
origin via replace.

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
- [`../../.context/CONTEXT.md`](../../.context/CONTEXT.md) — parent state module contracts
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/spawn-finalization.md` — finalization authority lattice, `runner_exit_*` invariant, and reaper contract
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/state-system.md` — per-spawn state layout, locking, and reconciliation model
