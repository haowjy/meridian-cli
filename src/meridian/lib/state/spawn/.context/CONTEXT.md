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

`transitions.py` functions (`apply_launch_facts`, `apply_finalize`) contain no I/O. Each
takes a `SpawnRecord` and returns a new `SpawnRecord` with updated fields. Callers decide
whether to run on the owner hot path or inside `write_state_locked`'s mutator lambda, then
pass the same transition function either way.

Rows are created `running` and have one status write after that: the terminal, in the same
write as the `terminal` facts. `apply_launch_facts` only records launch facts (launch mode,
runner and worker PIDs). `queued` and `finalizing` stay readable on rows older builds
wrote; no status transition table remains, since the one write is active → terminal.

## Harness Exit vs Runner-Exit vs Terminal Facts

New rows carry one exit record, `terminal`. `runner_exit` and `last_attempt_exit_code` /
`last_attempt_exited_at` are read-only legacy fields: only older builds write them, and
cancel's convergence and `derive` still read them from such rows.

### Runner terminal intent: `runner_exit: RunnerExitFacts | None` (legacy)

`runner_exit` was intent for an older live runner's writer path, not authority for a dead
launch. A lock-backed dead launch is claimed through `claim_dead_launch()`; the free
runner-lifetime lock and cancel intent determine its derived outcome. The runner now
resolves cancel-versus-report in memory and writes the terminal once
(`complete_execution()` → `finalize()`). If it crashes first,
`SpawnApplicationService.finalize_dead_launch()` claims the free launch lock and persists
`cancelled` or `failed`/`crashed`.

### Finalized outcome: `terminal: TerminalFacts | None`

Frozen sub-model holding the complete finalized state (`exit_code`,
`finished_at`, `published_at`, `duration_secs`, token/cost metrics, `error`,
`origin`). Written by `apply_finalize()` in `transitions.py`. Does not carry
status; top-level `status` is the sole status authority. A *derived* terminal (dead launch,
no stored terminal) takes `finished_at` from an older exit time on the row, else the newest
mtime of the files only the runner appends to (`LaunchPaths.runner_activity`: output, stderr,
debug, runner lifecycle log, report), else `started_at`: never `""`, and the
same on every read.

### PID-liveness hardening: `runner_created_at_epoch`

`psutil.Process(runner_pid).create_time()` captured alongside `runner_pid`. Cancel signals a
PID only on an exact birth match (`signal_with_birth`); a row with no recorded birth is
never signalled. Only a lockless legacy row falls back to `legacy_runner_alive`'s start grace.

## Terminal Write Authority

`spawn_store.py:finalize_spawn()` writes a terminal only to an active row, under the
per-spawn lock; a row that already has one declines the write. Every terminal writer holds
the launch lock (the runner, or the claim a continue, cancel, doctor or the repair pass
takes), except a lockless legacy row's older runner.

## Anti-Patterns

**Don't read raw `state.json` directly** — use `read_state()`. Raw reads bypass
Pydantic validation and miss the prompt-body stitch from `starting-prompt.md`.

**Don't call `write_state()` from external processes** — use `write_state_locked()`
with a mutator. Without the lock, external writes race with the runner's hot path.

**Don't treat `last_attempt_exit_code == 0` as a spawn-success signal.** It is
legacy pre-finalization bookkeeping from older builds and can disagree with the final
outcome. The outcome lives in `terminal` and the top-level `status`.

**Don't use `durable_report` or `runner_exit` to classify a dead lock-backed
launch.** `read_derived()` and dead-launch finalization ignore both: cancel intent
yields `cancelled`; otherwise the free launch lock yields `failed`/`crashed`. Do not
invent a separate orphan branch from a missing `runner_exit`.

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
