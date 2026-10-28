# lib/launch/ — Composition and Execution

Sits between the policy layer (`ops/spawn/`) and the mechanism layer (`harness/`).
Turns a `SpawnRequest` into a running process. Owns the composition seam that wires
policy to mechanism; not what to spawn, not how to build argv for a given harness.

## The Composition Seam

All launch state (argv, env, permissions, prompt, workspace roots) is assembled by one
pipeline in `context.py`. No driving adapter composes any of it independently, and none
reconstructs argv, env, or permissions from the request: they consume the bound result.
Two paths that compose separately diverge silently.

```
SpawnRequest + LaunchRuntime
  → compile_prepared_policy_surface()   model/harness/profile resolution
  → prepare_launch_surface()            skills, prompt, content (expensive: mars calls)
  → PreparedLaunchSurface
  → bind_launch_context()               env, cwd, spec, argv, permissions
  → LaunchContext                       bound plan; replaced, not mutated, after bind
  → materialize_launch_context()        real-launch-only adapter writes
```

Prepare once, bind as often as needed: the primary path binds twice (`dry_run=True`
preview, then real). `bind_launch_context()` writes nothing, but ignoring an active
direnv environment runs a bounded subprocess on every bind, previews included.

- Callers first resolve work, `--from` inheritance, task cwd, reference anchors and `-f`
  with `resolution.py:resolve_launch_inputs()`; don't hand-roll that.
- Exact continue (`meridian --continue`, `spawn --continue`) goes through
  `continue_replay.py` (`ContinueReplayContract`), shared by primary and spawn.
  Persisted `LaunchPolicySnapshot.model == ""` is legacy JSON for harness-default; it
  is normalized to `None` in `policy_snapshot.py` only.

### Driving adapters

1. Primary CLI: `launch/__init__.py:launch_primary()`.
2. Spawn: `ops/spawn/execute.py` calls `compose_spawn_launch_surface()` once, then
   `bind_spawn_launch_context()` for preview and execute; foreground and background
   converge at `launch_prepared_spawn()`.
3. Streaming-serve: `cli/streaming_serve.py` prepares via
   `SpawnApplicationService.prepare_spawn()` (row created only after preparation
   succeeds), then `run_streaming_spawn()`.

All runners call `bind_entry()` before exec and `conclude_native_run()` after teardown,
once per turn, in order: IDs → adapter → boundary → attribution (details:
`process/AGENTS.md`). Invocation attribution follows successful identity conclusion,
never row creation or startup.

### Composition surfaces

`LaunchCompositionSurface` on `LaunchRuntime` decides whether Mars `launch-bundle` runs:
`SPAWN_PREPARE` and `PRIMARY` run it; `DIRECT` skips policy and trusts the request (tests
only, via `tests/support/launch.py`). Production spawn paths build the runtime with
`build_spawn_mars_runtime` in `plan.py`. Persisted `SpawnRequest.model` stays the CLI token;
`binding.spec.model` comes from Mars `harness_model` at bind. Re-compose only when
session/fork resolution mutates the request.

Use `LaunchArgvIntent.SPEC_ONLY` on execution paths; `REQUIRED` is only for
`ops/spawn/prepare.py` dry-run display.

## Key Types

- `SpawnRequest` / `LaunchRuntime` — frozen Pydantic DTOs, JSON-safe fields only. No
  `Path` on `SpawnRequest`: the background worker persists it to disk and `Path` breaks
  the round-trip. It carries resolved display metadata (`skill_paths`, `cli_command`,
  `selection_report`, `permission_report`...) that executors must not compose from.
- `PreparedLaunchSurface` — preparation output; excludes anything that varies per bind.
- `RuntimeBindings` — what differs between preview and real bind: `spawn_id`,
  `runtime_work_id`, `chat_id`, `dry_run`. Report and system-prompt paths derive from
  `spawn_id` in bind.
- `LaunchContext` — frozen, fully composed. `warnings` is the only channel for
  composition warnings; stderr or logs are invisible to callers.

## Finalization Ownership

Three concentric layers: the runner's `finally` (`execute_with_streaming`) covers
partial setup; `launch_prepared_spawn`'s `except` finalizes a launch failure and keeps
typed native-unavailable reasons; a surface backstop wraps the whole post-row section.
`complete_spawn()` is idempotent, which makes the overlap safe.

## Execution

A harness turn executes once: terminal frames, transport failures, timeouts, guardrails
and identity failures finalize the attempt with no launch-level replay. Reports, usage and
identity come from `AttemptFacts` folded on live events. Pre-connect span is bounded by
`_start_spawn_with_timeout()` in `streaming_runner.py` (`timeouts.startup_minutes`).
Late runner diagnostics that create parents use the published-spawn artifact mutation
seam, never a direct append into a spawn directory retention may have deleted.

Stdin prompts need explicit `--prompt-file -`; there is no implicit stdin read. Empty
prompt is valid for `--continue` and reference-only launches.

## Rules

- **Fork after the row.** `fork.materialize_fork()` writes a session artifact that
  references the spawn ID; call it only after the spawn row exists (`fork.py` raises
  otherwise).
- **Resolve before persisting the spawn row.** Stated at
  [`../core/spawn_service.py`](../core/spawn_service.py) (`prepare_spawn`).
- **`--from` prior context goes in the user turn, not the system prompt.** Prior reports
  may hold user-generated content and the system prompt carries implicit authority.
  Use `resolve_task_context_inputs()`.
- **No per-launch values in the system prompt**: a spawn's run ID, launching chat and
  session links go in the first user turn (`build_launch_facts_prompt()`) so sibling
  spawns send byte-identical system bytes. A primary's own chat ID and links stay in the
  capability block (`--continue` replays no user turn); a primary gets no run-ID line,
  since no caller holds it and the chat ID is its handle. A `--goal` never creates a user
  turn for a primary. Rationale and the task-dir pin rules:
  [.context/CONTEXT.md](.context/CONTEXT.md).
- **Skills** come from the launch-bundle JSON, not from reading `.mars/skills/`;
  `tools.mcp` in the bundle is permission names, never MCP server config. Loading
  detail: [.context/CONTEXT.md](.context/CONTEXT.md).

## Depth

[.context/CONTEXT.md](.context/CONTEXT.md) — spawn cwd model (authority_root,
logical_task_cwd, actual_process_cwd), reference anchors, worktree ownership, workspace
projection, env injection, background-worker trust, exact-continue contract, Mars
bundle resolution, the user-turn context rationale.

Lock rules for runner lifetime (`launch.lock` held for the runner's life, stable lock
inodes, lock order): [../state/AGENTS.md](../state/AGENTS.md).

Related: [../ops/spawn/AGENTS.md](../ops/spawn/AGENTS.md) (policy layer),
[../harness/AGENTS.md](../harness/AGENTS.md) (mechanism layer).
