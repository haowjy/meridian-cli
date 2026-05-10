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

## Fork / Continue Cross-Harness Guard

`spawn_fork_sync()` and `spawn_continue_sync()` both resolve the effective target harness via a
dry-run `_resolve_effective_fork_target_harness()` call before spawning. If the source and target
harnesses differ, the operation raises `ValueError` — cross-harness continuation is not supported.

`spawn_fork_sync()` copies `goal` from the source spawn row when no `--goal` is provided.

## prepare.py — The One REQUIRED Path

`prepare.py` uses `LaunchArgvIntent.REQUIRED` on `LaunchCompositionSurface.SPAWN_PREPARE`. This is
the **only** execution path that builds a real argv — used for `--dry-run` display to populate
`cli_command` in `SpawnActionOutput`. All actual execution paths use `SPEC_ONLY`. Do not add
`REQUIRED` elsewhere.

## Background Spawn Trust Model

The background worker reads a persisted `BackgroundWorkerLaunchRequest` from disk. It trusts this
request entirely — it does not re-resolve model, harness, or profile. The request is already fully
resolved at persist time. An empty `model` field is accepted (model-optional profiles exist).

## SpawnActionOutput Wire Shape

`to_wire()` is for explicit `--format json` consumers (agents, scripts). `to_agent_wire()` is the
sparse shape used when agents run without an explicit format flag — it omits dry-run detail and
adds the `wait_required` / `terminal: false` fields for background spawns.

The distinction matters: agents that parse `to_wire()` without requesting it explicitly will
receive `to_agent_wire()` instead.

## Lateral Links

→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — ops/ layer overview, SpawnApplicationService, SEAM-1/2/3
→ [../../../launch/.context/CONTEXT.md](../../../launch/.context/CONTEXT.md) — launch mechanism ops/spawn drives
