# state/spawn — Context

Detailed contracts for spawn state persistence. The parent `.context/` covers
the dual-root layout and atomic write rules — this file covers the spawn-specific
model split, two-tier write discipline, and finalization policy.

## SpawnRecord vs StoredSpawnState

`SpawnRecord` (in `model.py`) is the in-memory projection used throughout the
codebase. `StoredSpawnState` (in `repository.py`) is the on-disk v2 `state.json`
representation.

Key difference: `SpawnRecord` carries the full prompt body in `prompt: str | None`.
`StoredSpawnState` stores only `prompt_length: int | None` — the prompt body lives
in a separate `starting-prompt.md` file. This keeps `state.json` reads lightweight
(no large strings in the JSON), and `read_state()` stitches them together.

`record_to_stored_state()` / `stored_state_to_record()` are the conversion functions.
`read_state()` calls both automatically; callers don't need the stored form directly.

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

## Attempt vs Terminal-Intent Fields

The spawn record carries two distinct categories of exit metadata. Confusing them
produces wrong terminal-state decisions.

### Attempt-level bookkeeping: `last_attempt_exit_code` / `last_attempt_exited_at`

Replaces `process_exit_code` / `exited_at`. Same write path — `apply_record_exited()`
in `transitions.py`, called after each harness attempt drains. Same semantics as
before, but the name makes the scope explicit: these are **attempt-level** values
overwritten on every retry. They carry no spawn-level terminal meaning.

| Aspect | Detail |
|---|---|
| Written by | `_run_streaming_attempt`, `run_streaming_spawn`, `run_harness_process` |
| Written when | After each attempt drains (per-attempt, not final) |
| Overwritten on retry | Yes — each new attempt replaces the prior value |
| Terminal meaning | None — a `0` exit code can precede retries or post-attempt budget failures |
| Readers | `SpawnDetailOutput` (display), session export duration fallback, reaper diagnostic context |

### Runner terminal intent: `runner_exit_code` / `runner_exit_status` / `runner_exit_error` / `runner_exit_at`

The runner's **resolved terminal outcome** — what the runner would have passed to
`complete_execution()` if it hadn't crashed. Written exactly once, after all attempts
and post-attempt work (guardrails, retry decisions, budget checks) are complete.

**Authoritative presence check:** `runner_exit_status is not None`. If `None`, the
entire tuple is treated as absent regardless of other `runner_exit_*` values. This
is the single unambiguous guard for the reaper.

| Aspect | Detail |
|---|---|
| Written by | `record_runner_exit()` on `SpawnLifecycleService` |
| Written when | After `terminal_facts` is resolved, BEFORE `mark_finalizing()` |
| Written how many times | Once per spawn — `write_state()` under signal masking |
| Values | `runner_exit_status`: `"succeeded"` \| `"failed"` \| `"cancelled"` |
| Reaper usage | Replaces the old `process_exit_code is not None` branch — reaper finalizes from the persisted decision, not from attempt evidence |

**Write sequence (caller contract):**

1. Resolve `terminal_facts` from `conclusion`
2. `record_runner_exit()` — persist resolved outcome (atomic write)
3. `complete_execution()` → `mark_finalizing()` → `finalize()`

Crash safety:
- Crash between 2 and 3: reaper sees `runner_exit_*` on `running` spawn → uses it.
- Crash between 3's `mark_finalizing()` and `finalize()`: reaper sees `runner_exit_*` on `finalizing` spawn → uses it.
- Crash before 2: reaper sees no `runner_exit_*` → orphan-fails. Correct.

**Writers must cover all terminal paths:** `execute_with_streaming()` (primary),
`_finalize_lifecycle_and_observe_session()` (process runner), and `streaming_serve.py`
CLI path. Consistency across all paths is mandatory — inconsistent coverage produces
orphan false-failures for some launch modes.

### PID-liveness hardening: `runner_created_at_epoch`

`psutil.Process(runner_pid).create_time()` captured at spawn start alongside
`runner_pid`. Passed to `is_process_alive()` as `created_after_epoch` for robust
PID-birth-time verification.

The prior heuristic used `started_at` with a 30s grace period — fragile under
delayed launch (runner starting >30s after spawn creation). `runner_created_at_epoch`
makes this exact.

| Aspect | Detail |
|---|---|
| Captured when | `mark_running` / `start_spawn` — wherever `runner_pid` is set |
| Value | Unix epoch float from `psutil.Process(os.getpid()).create_time()` |
| Fallback | `None` if `psutil.AccessDenied` or `psutil.NoSuchProcess` — caller falls back to `started_at` heuristic |
| Cross-platform | `psutil.Process.create_time()` works on Windows, Linux, macOS |

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
runner's terminal intent lives in `runner_exit_status`, not in attempt exit codes.

**Don't trust `durable_report` as success evidence when `runner_exit_status is None`
and the runner is dead.** A `report.md` from a prior guardrail-failing attempt looks
identical to one from a successful run. The runner is the only entity that knows
whether the spawn succeeded — if it never persisted `runner_exit_*`, treat the spawn
as an orphan failure even if artifacts exist.

## Related

- [`../AGENTS.md`](../AGENTS.md) — entry points
- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — parent state module contracts
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/spawn-finalization.md` — finalization authority lattice, `runner_exit_*` invariant, and reaper contract
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/state-system.md` — per-spawn state layout, locking, and reconciliation model
