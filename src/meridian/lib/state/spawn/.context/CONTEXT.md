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

## Two-Tier Write Model

**Tier 1 — Owner writes (`write_state`):** The spawn's own runner is the sole
writer while active. Calls `write_state()` without acquiring `state.lock`. Performs
a best-effort terminal monotonicity guard: reads current on-disk state, refuses to
overwrite an already-terminal record unless `allow_terminal_overwrite=True`.

**Tier 2 — External writes (`write_state_locked`):** The reaper, cancel command,
or any process that needs to mutate a spawn it doesn't own. Acquires `state.lock`,
reads current state, applies a mutator function, writes atomically. Prevents torn
writes when multiple processes compete.

The distinction is enforced by convention. An external writer that skips `state.lock`
races with the owner's unlocked writes.

## Pure Transitions

`transitions.py` functions (`apply_mark_running`, `apply_record_exited`,
`apply_mark_finalizing`, `apply_finalize`) contain no I/O. Each takes a `SpawnRecord`
and returns a new `SpawnRecord` with updated fields. Callers decide whether to run
on the owner hot path or inside `write_state_locked`'s mutator lambda, then pass the
same transition function either way.

Status transitions are validated against the allowed state machine in
`core.spawn_lifecycle.validate_transition()`. Pass `validate_status_transition=False`
only when the record may be in `unknown` status (legacy migration paths).

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

## Related

- [`../AGENTS.md`](../AGENTS.md) — entry points
- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — parent state module contracts
