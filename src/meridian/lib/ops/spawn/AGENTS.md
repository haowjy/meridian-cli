# Spawn policy

Contracts, wait scoping and delivery: [.context/CONTEXT.md](.context/CONTEXT.md).

## Rules

- **Resolve before persist** ([../AGENTS.md](../AGENTS.md)): in `api.py`, runtime
  materialization waits until the resolved request's sandbox passes the caller guardrail,
  for every caller kind.
- **Foreground and background converge at `launch_prepared_spawn()`**, which owns
  pre-run failure finalization (writes `launch_failure`, then re-raises; safe because
  `complete_spawn()` is idempotent).
- **Depth guard is ops-layer.** `max_depth_reached()` runs before any spawn execution
  and returns `depth_exceeded_output()` with no row; `launch/` does not enforce depth.
- **Import execution entry points from `execute.py` only**, never `execute_*` from
  outside `ops/spawn/`.
- **Observation is opt-in for pure readers.** Result fetchers that don't consume a
  parent's completion notification use `observe=False` (`--no-observe`). Show, status,
  report-show, cancel, and cancel-all default false so internal, UI, and MCP callers stay
  pure; the CLI opts in. Normal waits observe by default; formatting a notification is
  not delivery. `-C` invocations supply no observer spawn ID, and `ack` is refused.
- **Choose status by intent**: `read_derived()`/`overlay_derived()` for liveness,
  display, wait, idle; stored status for cancel convergence, archive, destruction.
- **No cross-harness fork/continue**; the guard raises before any state is created.
  `spawn_continue` does not assemble replay policy: it delegates to
  `launch/continue_replay.py` (`ContinueReplayContract`,
  [contract](../../launch/.context/CONTEXT.md)), allows only model
  overrides, and treats agent opt-out as a launch-identity mutation. Native-ID
  continuation never substitutes an owner's newest spawn.
- **A continue keeps the spawn ID.** It prepares before reopening under the
  launch lock, so a setup error leaves no new row. It emits `spawn.started`,
  not `spawn.created`.
- **A continue requires a prompt.** `spawn_continue_sync` rejects an empty or whitespace
  prompt first, before any resolution. There is no fallback to the source's original prompt:
  resending it would redo the task.
- **`spawn_wait` no-arg owed rows** (`owed.py`) are separate from `spawns` in output and
  never affect fail-fast, aggregates, or exit status; `--fail-fast` triggers on `failed`
  or `timed_out`, not `cancelled`.
