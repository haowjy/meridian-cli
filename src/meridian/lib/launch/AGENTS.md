# lib/launch/ — Composition and Execution

Sits between the policy layer (`ops/spawn/`) and mechanism layer (`harness/`).
Turns a `SpawnRequest` into a running process. Does not own policy decisions (what
to spawn) or harness specifics (how to build argv for Claude/Codex) — it owns the
composition seam that wires them together.

## Skill Loading

Mars-resolved skill content comes from the launch-bundle JSON (`skills.loaded[].{name, skill_type, body}`),
not from reading `.mars/skills/` on disk. `_build_resolved_skills_from_bundle()` in `policies.py`
constructs `ResolvedSkills` from bundle data and preserves `source_path` for snapshots and
workspace projection. This includes bundled Meridian skills when the profile loads their
Mars-provided copies. It reconstructs a primary-root path only for older producers that omit
provenance. Snapshot replay uses only its persisted `loaded_skills`; an empty field means no
skills are loaded.

Meridian-owned operational skills stay in the wheel and are provided into the
resolved Mars root before each launch. `bundled_skills.py` owns both the wheel
inventory and the source a named `meridian skills` read may use. Names Mars reports
as provided read from `.mars/skills/`; all others read from the wheel. Provided
skills never enter harness-native skill directories.

Wheel metadata builds the `# Meridian skills` trigger block. A skill already loaded
by the Mars launch bundle has no trigger line, preventing a loaded body and a pointer
to a different copy from coexisting. Otherwise, optional `metadata.mode` frontmatter gates
the trigger on the resolved profile's declared mode, independent of whether the
launch is a primary session or a spawn. The notify capability line uses the same
mode gate. `meridian skills NAME` renders the selected copy under the same activation
header injected skills get (`render_skill_block()`).

The `bundle_adapter.py` module docstring inventories bundle fields Meridian deliberately does
not read. `tools.rules` is the permission contract; `tools.mcp` is a flat list of permission
names, never MCP server configuration.

## The Composition Seam (Invariant I-1)

All launch state assembly — argv, env, permissions, prompt, workspace roots — happens
through the prepare/bind pipeline in `context.py`. No driving adapter composes these
independently. This is a hard invariant; violation means two code paths can diverge silently.

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
        │                             (may invoke direnv when ignoring an active environment)
        ▼
LaunchContext                         ← pure bound plan
        │
        ▼
materialize_launch_context()          ← real-launch-only adapter writes
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
does not write; ignoring an active direnv environment invokes a bounded subprocess
on each bind, including previews.

## Three Driving Adapters

Three code paths enter the launch composition seam — each has a defined
prepare/bind entry:

1. **Primary CLI** (`launch/__init__.py:launch_primary()`): `prepare_launch_surface()`
   once, then `bind_launch_context()` twice — first for dry-run display, second for
   `run_harness_process()`.
2. **Spawn subprocess** (`ops/spawn/execute.py`): `compose_spawn_launch_surface()`
   once per operation, then `bind_spawn_launch_context()` for preview and execute.
   Foreground and background paths converge at `launch_prepared_spawn()`.
3. **CLI streaming-serve** (`cli/streaming_serve.py`): resolve-before-persist via
   `SpawnApplicationService.prepare_spawn()`, then `run_streaming_spawn()`. Row
   created only on successful preparation. All three runners use `bind_entry()`
   before exec and `conclude_native_run()` after teardown. Invocation attribution
   follows successful identity conclusion, never merely row creation or startup.

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
2. **Helper** (`launch_prepared_spawn`): `except` finalizes a launch failure, preserving
   typed native-unavailable reasons in the terminal error; safe
   because `complete_spawn()` is idempotent.
3. **Surface backstop**: last-resort around the entire post-row section.

## Hard Invariants

| Code | Rule |
|---|---|
| I-1 | All composition uses the prepare/bind pipeline — no adapter composes independently |
| I-2 | No driving adapter reconstructs argv, env, or permissions independently |
| I-4 | `conclude_native_run()` once per turn after teardown joins: IDs → adapter → boundary → attribution |
| I-5 | `SpawnRequest`/`LaunchRuntime` carry no derived state; `LaunchContext` complete at construction |
| I-10 | Fork materialization (`fork.py`) happens only after spawn row exists |
| I-13 | `LaunchContext.warnings` is the sole channel for composition warnings |

## Composition Surfaces

`LaunchCompositionSurface` on `LaunchRuntime` controls whether Mars `launch-bundle` runs:

| Surface | Mars? | Use |
|---------|-------|-----|
| `SPAWN_PREPARE` | Yes | Spawn prepare dry-run, spawn execute (fg/bg), streaming-serve prepare — needs `harness_model` |
| `PRIMARY` | Yes | Interactive primary |
| `DIRECT` | No | Tests only, via `tests/support/launch.py`; `resolve_launch_policy` rejects it without a prepared surface |

`DIRECT` does not mean “pass model to Mars”; it means skip policy and trust the request.
`SPAWN_PREPARE` passes effective `deny_headless_harnesses` to Mars as caller
exclusions before selection; `PRIMARY` does not. The final headless guard remains
in place and does not select a replacement route.
Production spawn paths that need Mars must use `build_spawn_mars_runtime` in `launch/plan.py`
(`SPAWN_PREPARE` + config snapshot). Spawn uses **prepare-once / bind-twice**: `compose_spawn_launch_surface`
once per operation, then `bind_spawn_launch_context` for preview (dry-run `REQUIRED` argv) and execute
(`SPEC_ONLY`). Persisted `SpawnRequest.model` stays the canonical CLI token; `binding.spec.model` comes
from Mars `harness_model` at bind. Re-compose only when session/fork resolution mutates the request.

Use `LaunchArgvIntent.SPEC_ONLY` on execution paths. `LaunchArgvIntent.REQUIRED` is only for
`ops/spawn/prepare.py` dry-run display (needs real argv for `cli_command`). **Do not set
`REQUIRED` on execution paths.**

## Startup Watchdog

`_start_spawn_with_timeout()` in `streaming_runner.py` wraps the entire pre-connect
span — backend boot, connection, and session handshake — with an outer
`asyncio.timeout`. Default bound is 5 minutes, configured via
`timeouts.startup_minutes` or `MERIDIAN_STARTUP_TIMEOUT_MINUTES`. Both the spawn
path (`execute_with_streaming`) and the streaming-serve path (`run_streaming_spawn`)
use the same helper. Exceeding the bound raises `StartupPhaseTimeout`, which the
runner preserves as the turn's terminal failure.

Rationale: before the watchdog, the pre-connect span was unbounded by accident. The
recorded worst case was a spawn that wedged for 2h18m before any liveness signal.

## Single-Attempt Execution

A Meridian harness turn executes once. Terminal frames, transport failures,
timeouts, guardrails, identity failures, and generic subprocess failures all
finalize that attempt without launch-level replay. Reports, usage, and identity
come from `AttemptFacts` folded on live events; explicit terminal errors remain
the causal final result. Transport-owned readiness polling may retry only within
an adapter when the operation is demonstrably idempotent.

Runner lifecycle diagnostics can execute after async boundaries. Their
parent-creating writes use the published-spawn artifact mutation seam; never append
late diagnostics directly into a spawn directory that retention may have deleted.
Streaming budget checks observe typed cumulative `AttemptFacts.usage` immediately
after the inline fold, before subscriber fan-out. Harness wire costs must first
be normalized by the harness fold; a subscriber backlog cannot hide a breach.

## Prompt Source

Stdin prompt reading requires explicit `--prompt-file -`. The implicit stdin
fallback (the old `sys.stdin.read()` path) was deleted because it caused spawns to
hang indefinitely when fd 0 was held open without data. Empty prompt is valid for
`--continue` / reference-only launches.

## Key Entry Points

- `context.py` — `prepare_launch_surface()`, `bind_launch_context()` — the seam
- `__init__.py` — `launch_primary()` for the interactive primary path
- `streaming_runner.py` — `execute_with_streaming()` for spawn/streaming paths;
  `_start_spawn_with_timeout()` for the startup watchdog
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

**Don't put per-launch values in the system prompt.** Per-launch values stay out;
per-conversation values may stay in. Every execution has its own chat and so its own
two links — `sessions/<chat>/work` and `sessions/<chat>/task` (`session_links.py`) —
and their paths are per-launch for a spawn and per-conversation for a primary. So a
spawn's run ID, its launching chat, and those two links go in the first user turn
(`build_launch_facts_prompt()`), never the system prompt: sibling spawns then send
byte-identical system bytes. A primary's own chat ID and its two links stay in the
capability block, because a `--continue` replays no user turn. Bind also pins a
checkout no store can recompute — this run's `--task-dir`, a launch from outside the
project, or the pin its launcher passed on (read from the launcher's spawn scope, never
from `MERIDIAN_TASK_DIR`) — in the execution's spawn scope before it publishes the
links, and nothing else: a checkout that came from the item or the project root follows
`work task-dir` and `work switch`, and `meridian task-dir clear` removes the pin. A spawn's `--goal`
contract is its task turn (`_build_goal_instruction`'s text); a primary's is a
guidance block, because goal text must never create a user turn, or a bare primary
submits the goal as its first message, fresh and on every resume. A continued
conversation inherits the goal from its row (`SpawnRecord.goal`), the way a continued
spawn does. The work item's own goal is a pointer instead — `meridian work show`
resolves the current item, so neither the text nor an ID that could go stale enters
the prompt. An exact continue recomposes the system prompt from the launch snapshot
(profile, loaded and available skills, inventory, execution policy, tools); within one
Meridian version identical inputs produce identical bytes, so a fresh launch shares
the cached prefix with its siblings and a continue keeps the conversation's. Only a
real identity change — a new goal, surface, or config — may change them. Foreground
and background spawns share one preamble for the same reason.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — spawn CWD three-directory model
   (authority_root, logical_task_cwd, actual_process_cwd), the one bind-rendered capability
   guidance, reference loading and anchor semantics (`kb:` prefix, `@` unsupported),
   worktree path assignment vs managed ownership separation, workspace projection
   detail, env injection, background worker trust model, _MERIDIAN_HARNESS child env
   behavior, exact continue replay contract, full invariant table, Mars launch-bundle
   policy resolution, user-turn context threading
   (`resolve_task_context_inputs()`) and why prior-context goes in the user turn.

## Related

- `../ops/spawn/AGENTS.md` — policy layer that drives this layer
- `../harness/AGENTS.md` — mechanism layer this calls for `project_content()`, `build_launch_argv()`
- KB `architecture/launch-system.md` — launch adapter architecture
- KB `concepts/spawn-lifecycle.md` — spawn status machine, crash recovery
