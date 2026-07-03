# lib/launch/ — Composition and Execution

Sits between the policy layer (`ops/spawn/`) and mechanism layer (`harness/`).
Turns a `SpawnRequest` into a running process. Does not own policy decisions (what
to spawn) or harness specifics (how to build argv for Claude/Codex) — it owns the
composition seam that wires them together.

## Skill Loading

Skill content comes from the Mars launch-bundle JSON (`skills.loaded[].{name, skill_type, body}`),
not from reading `.mars/skills/` on disk. `_build_resolved_skills_from_bundle()` in `policies.py`
constructs `ResolvedSkills` from bundle data. `resolve_skills_from_profile()` is kept only for
legacy snapshot replay when a persisted snapshot's `loaded_skills` field is empty.

## The Composition Seam (Invariant I-1)

All launch state assembly — argv, env, permissions, prompt, workspace roots — happens
inside `build_launch_context()` in `context.py`. No driving adapter composes these
independently. This is a hard invariant; violation means two code paths can diverge silently.

`build_launch_context()` is a backward-compat wrapper over a two-phase pipeline:

```
SpawnRequest + LaunchRuntime
        │
        ▼
compile_prepared_policy_surface()     ← model/harness/profile resolution
        │
        ▼
prepare_launch_surface()              ← skill injection, prompt, content composition
        │                             (expensive: mars calls, profile loading)
        │  PreparedLaunchSurface
        │
        ▼
bind_launch_context()                 ← env, cwd, spec, argv, permissions
        │                             (cheap: microseconds)
        ▼
LaunchContext                         ← complete at construction; ready to run
```

Before callers enter this pipeline, they must resolve the shared launch-input
surface with `resolution.py:resolve_launch_inputs()`. That seam owns work
precedence, `--from` work inheritance, task cwd selection, reference anchoring,
and `-f` validation. Primary and spawn may differ in prompt/session projection,
but they should not hand-roll directory or reference resolution.

Exact continue (`meridian --continue`, `spawn --continue`) enters through
`continue_replay.py`. `ContinueReplayContract` is the launch-owned seam that
primary and spawn share for replaying source session identity, launch-policy
snapshot, work, and task directory. Persisted `LaunchPolicySnapshot.model == ""`
is legacy JSON for harness-default model; replay normalizes it to in-memory
`None` in `policy_snapshot.py`, not in continue-specific callers.

**Why the split?** The primary CLI path calls `bind_launch_context()` twice: once
with `dry_run=True` for `--dry-run` display, then again with real spawn ID and paths.
`prepare_launch_surface()` is expensive and safe to call once. `bind_launch_context()`
is cheap and idempotent.

## Four Driving Adapters

Four code paths call `build_launch_context()` — each has a defined entry:

1. **Primary CLI** (`launch/__init__.py:launch_primary()`): prepare-once/bind-twice.
   First bind for dry-run display, second for `run_harness_process()`.
2. **Spawn subprocess** (`ops/spawn/execute.py`): foreground and background paths
   both converge at `launch_prepared_spawn()`.
3. **REST app** (`lib/app/spawn_routes.py`): resolve-before-persist via
   `SpawnApplicationService.prepare_spawn()`. Row created only on success.
4. **CLI streaming-serve** (`cli/streaming_serve.py`): also through
   `SpawnApplicationService.prepare_spawn()`, then `run_streaming_spawn()`.

## Key Types

- `SpawnRequest` / `LaunchRuntime` — frozen Pydantic DTOs; JSON-safe fields only.
  No `Path` on `SpawnRequest` — it gets persisted to disk by the background worker.
- `PreparedLaunchSurface` — preparation output; carries resolved request, skills, refs.
  Excludes spawn IDs, report paths, env — everything that varies per bind.
- `RuntimeBindings` — the only things that differ between preview and real bind:
  `spawn_id`, `runtime_work_id`, `chat_id`, `dry_run`. The report/system-prompt
  paths are derived in bind from `spawn_id` (spawn log dir), not threaded.
- `LaunchContext` — fully composed; complete at construction, ready to hand to an executor.

## Finalization Ownership

Three concentric layers, each defined by function scope:
1. **Runner** (`execute_with_streaming`): `finally` handles partial-setup failures.
2. **Helper** (`launch_prepared_spawn`): `except` writes `launch_failure`; safe
   because `complete_spawn()` is idempotent.
3. **Surface backstop**: last-resort around the entire post-row section.

## Hard Invariants

| Code | Rule |
|---|---|
| I-1 | All composition inside `build_launch_context()` — no adapter composes independently |
| I-2 | No driving adapter reconstructs argv, env, or permissions independently |
| I-4 | `observe_session_id()` called exactly once post-execution (primary path only) |
| I-5 | `SpawnRequest`/`LaunchRuntime` carry no derived state; `LaunchContext` complete at construction |
| I-10 | Fork materialization (`fork.py`) happens only after spawn row exists |
| I-13 | `LaunchContext.warnings` is the sole channel for composition warnings |

## Composition Surfaces

`LaunchCompositionSurface` on `LaunchRuntime` controls whether Mars `launch-bundle` runs:

| Surface | Mars? | Use |
|---------|-------|-----|
| `SPAWN_PREPARE` | Yes | Spawn prepare dry-run, spawn execute (fg/bg), streaming-serve prepare — needs `harness_model` |
| `PRIMARY` | Yes | Interactive primary |
| `DIRECT` | No | Tests and truly pre-resolved `SpawnRequest` only |

`DIRECT` does not mean “pass model to Mars”; it means skip policy and trust the request.
Production spawn paths that need Mars must use `build_spawn_mars_runtime` in `launch/plan.py`
(`SPAWN_PREPARE` + config snapshot). Spawn uses **prepare-once / bind-twice**: `compose_spawn_launch_surface`
once per operation, then `bind_spawn_launch_context` for preview (dry-run `REQUIRED` argv) and execute
(`SPEC_ONLY`). Persisted `SpawnRequest.model` stays the canonical CLI token; `binding.spec.model` comes
from Mars `harness_model` at bind. Re-compose only when session/fork resolution mutates the request.

Use `LaunchArgvIntent.SPEC_ONLY` on execution paths. `LaunchArgvIntent.REQUIRED` is only for
`ops/spawn/prepare.py` dry-run display (needs real argv for `cli_command`). **Do not set
`REQUIRED` on execution paths.**

## Model-Policy Overlay Semantics

Overlay rules (from `AgentOverlayConfig.model_policies`) **prepend** to profile rules —
they do not replace them. Effective list = overlay rules first, profile rules second.
First rule whose selector matches wins; later rules with the same selector are silently
unreachable.

Harness-availability fallback walks the combined list in order, skipping rules with
`no_fallback=True` and rules with `match_type == "model-glob"` (always excluded).
The demoted base candidate (pre-policy-transform) is tried before the policy list
when a policy rule caused the primary harness selection.

Source: `compiler.py:effective_model_policies()`, `policies.py:_fallback_candidates_from_policies()`.
Detail: [.context/CONTEXT.md](.context/CONTEXT.md#model-policy-overlay-composition-policiespy).

## Key Entry Points

- `context.py` — `prepare_launch_surface()`, `bind_launch_context()` — the seam
- `__init__.py` — `launch_primary()` for the interactive primary path
- `streaming_runner.py` — `execute_with_streaming()` for spawn/streaming paths
- `process/` — `run_harness_process()` for the PTY/pipe primary executor

## Anti-Patterns

**Don't call `fork.materialize_fork()` before the spawn row exists.** Fork writes a
session artifact that references the spawn ID — the row must exist first (I-10).

**Don't route composition warnings to stderr or logs.** They go to
`LaunchContext.warnings` (I-13). Other channels are invisible to callers.

**Don't add `Path` fields to `SpawnRequest`.** The background worker serializes
`SpawnRequest` to disk — `Path` breaks JSON round-trip.

**Don't use stdlib `logging` in `launch/`.** Use `structlog.get_logger()`.
`capture_library_diagnostics()` captures stdlib warnings during spawn; structlog
bypasses it and leaks to stderr.

**Don't put `--from` prior-context in the system prompt.** Prior spawn reports may
contain user-generated content; the system prompt is an instruction channel with
implicit authority. Use `resolve_task_context_inputs()` to land it in the user turn.
Variable content in the system prompt also destroys prompt-cache locality.
See [.context/CONTEXT.md](.context/CONTEXT.md#why-user-turn-not-system-prompt) for the full rationale.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — spawn CWD three-directory model
   (authority_root, logical_task_cwd, actual_process_cwd), task CWD instruction
   injection, reference loading and anchor semantics (`kb:` prefix, `@` unsupported),
   worktree path assignment vs managed ownership separation, workspace projection
   detail, env injection, background worker trust model, MERIDIAN_HARNESS child env
   behavior, exact continue replay contract, full invariant table, model-policy
   overlay composition and fallback chain rules, user-turn context threading
   (`resolve_task_context_inputs()`) and why prior-context goes in the user turn.

## Related

- `../ops/spawn/AGENTS.md` — policy layer that drives this layer
- `../harness/AGENTS.md` — mechanism layer this calls for `project_content()`, `build_launch_argv()`
- KB `architecture/launch-system.md` — full four-adapter diagram
- KB `concepts/spawn-lifecycle.md` — spawn status machine, crash recovery
