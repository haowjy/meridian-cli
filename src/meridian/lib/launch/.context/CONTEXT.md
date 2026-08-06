# launch/ — Composition and Execution

## Architecture

### The Prepare/Bind Composition Seam (Invariant I-1)

Runtime launch composition happens once inside `bind_launch_context()` in `context.py`.
It produces the complete argv, environment, cwd, and permissions consumed by the
driving adapter and its connection. No adapter recomposes the child environment
from `os.environ`. `build_launch_context()` remains the convenience wrapper that
prepares and binds in one call.

`build_launch_context()` is a backward-compat wrapper over a two-phase pipeline:

```
SpawnRequest + LaunchRuntime
        │
        ▼
compile_prepared_policy_surface()  ← model/harness/profile resolution (before prepare)
        │
        ▼
prepare_launch_surface()           ← skill injection, prompt, content composition
        │
        │  PreparedLaunchSurface (frozen dataclass — public boundary)
        │
        ▼
bind_launch_context()              ← env, cwd, spec, argv, permissions
        │
        ▼
LaunchContext                      ← complete at construction; ready to run
```

**Why the split?** The primary CLI path needs to call `bind_launch_context()` twice:
once with `dry_run=True` for `--dry-run` display, then again with real spawn-ID and
paths for actual execution. `prepare_launch_surface()` is expensive (mars calls, profile
loading, prompt assembly) and safe to do once before the session is opened.
`bind_launch_context()` is cheap (microseconds) and called as many times as needed.

### PreparedLaunchSurface

The in-memory boundary between preparation and binding. Carries: resolved request,
harness, composition warnings, loaded references, agent inventory prompt, context
prompt, alias catalog, model selection context. Deliberately excludes spawn IDs,
report paths, env, argv, and permission outputs — everything that varies per bind.

`launch_request` on `PreparedLaunchSurface` preserves the original pre-resolution
request for `LaunchContext.request` compatibility. `request` carries the resolved
version used by `bind_launch_context()`.

### RuntimeBindings

Frozen dataclass for runtime-only values: `spawn_id`,
`runtime_work_id`, `chat_id`, `forked_harness_session_id`, `plan_overrides`, `dry_run`.
These are the only things that differ between the preview bind and the real bind.
The report and `system-prompt.md` paths are derived in `bind_launch_context` from
`spawn_id` (the spawn log dir), not carried here.

### Exact Continue Replay

Primary `meridian --continue` and subagent `spawn --continue` are exact
continuations. `continue_replay.py` owns the launch-level seam:
`ContinueReplaySource` normalizes the resolved reference shape, and
`ContinueReplayContract` carries the launch-ready result consumed by primary and
spawn continue. Callers pass the authoritative recovered harness session ID into
the source; recovery policy stays in `ops/reference.py`, not in launch.

The contract replays the source task directory, work attachment, session identity,
and launch-policy snapshot rather than resolving from the caller's current
CWD/config/env. The absence of source work is a preserved value: exact continue
suppresses inherited `MERIDIAN_ACTIVE_WORK_*` and inherited task-dir state when the
source had none, instead of attaching the caller's ambient work/task context.

Launch-policy snapshot replay preserves cache- and prompt-shaping fields: model,
harness, agent, agent opt-out, agent profile, skills, loaded skill content,
execution policy, tool/MCP grants, terminal surface mode, matched policy rule,
fallback chain, inventory prompt, env, and passthrough args. A persisted snapshot
with `model=""` and a harness is valid legacy JSON; policy snapshot replay
normalizes it to in-memory `model=None`, meaning "omit Meridian's managed model
override and let the harness default." This normalization belongs in
`policy_snapshot.py`, not in continue-specific code.

Exact continue rejects launch-identity, policy, work, and task mutations. That
includes model, agent, skills, execution policy, passthrough args, env, `--work`,
`--task-dir`, and agent opt-out (`--agent ''`) because opt-out changes how default
agent routing behaves. Use a divergent mode instead.

KB: `decisions/launch.md#d-continue-replays-recorded-launch-contract-same-session-continue-is-not-live-policy-recomputation`.

### Three Driving Adapters

Three paths enter the launch composition seam:

1. **Primary CLI** (`launch/__init__.py:launch_primary()`): `prepare_launch_surface()`
   once, then `bind_launch_context()` twice — dry-run preview uses the first bind;
   `run_harness_process()` uses the second with real paths.

2. **Spawn subprocess** (`ops/spawn/execute.py`): `compose_spawn_launch_surface()`
   once per operation, then `bind_spawn_launch_context()` for preview and execute.
   Foreground calls `execute_spawn_blocking()` → `launch_prepared_spawn()` →
   `execute_with_streaming()`; background persists `BackgroundWorkerLaunchRequest`
   and detaches a worker subprocess which calls `_execute_existing_spawn()` →
   `launch_prepared_spawn()`. Both foreground and background converge at
   `launch_prepared_spawn()`, which owns pre-run failure finalization.

3. **CLI streaming-serve** (`cli/streaming_serve.py`): `SpawnApplicationService.prepare_spawn()`
   implements resolve-before-persist — launch composition runs before any spawn
   row is created. Row is only created on success (SEAM-1), then `run_streaming_spawn()`
   runs from `streaming_runner.py`.

### Finalization Ownership Layers

Three concentric layers inside the spawn path, each defined by function scope:

1. **Runner** (`execute_with_streaming`): sentinel locals initialized to `None`;
   `finally` block handles partial-setup failures.
2. **Helper** (`launch_prepared_spawn`): `except` catches pre-run exceptions, writes
   `launch_failure`; safe because runner's `complete_spawn()` is idempotent.
3. **Surface backstop**: last-resort around the entire post-row section in the
   calling surface function.

## Contracts

### DTO Discipline

- `SpawnRequest` and `LaunchRuntime`: frozen Pydantic, JSON-safe field types only.
  No `Path` on `SpawnRequest`. No `arbitrary_types_allowed`.
- `LaunchContext`: frozen dataclass. No pre-composed intermediate DTOs.
- No derived/cached state on any DTO — factory recomputes from inputs on each call.

Adding mutable state or `Path` fields to `SpawnRequest` breaks the JSON-safe
constraint required for the background worker's disk-persisted request.

### Invariants (from KB `architecture/launch-system.md`)

| Code | Rule |
|------|------|
| I-1 | Runtime composition happens at `bind_launch_context()`; `build_launch_context()` is its prepare+bind wrapper |
| I-2 | Driving adapters and connections consume the bound argv, env, and permissions without reconstruction |
| I-4 | `observe_session_id()` called exactly once post-execution (primary path only) |
| I-5 | `SpawnRequest`/`LaunchRuntime` carry no derived state; `LaunchContext` complete at construction |
| I-10 | Fork materialization (`fork.py`) happens only after spawn row exists |
| I-13 | `LaunchContext.warnings` is the sole channel for composition warnings |

These are checked on every PR touching `launch/`, `harness/`, `ops/spawn/`, `app/`,
or `cli/streaming_serve.py`.

### Composition Surfaces

`LaunchCompositionSurface` on `LaunchRuntime` controls which projection path runs
inside `prepare_launch_surface()`:

- `PRIMARY` — interactive session; uses `harness.seed_session()`, normalizes passthrough args
- `SPAWN_PREPARE` — subagent spawn; full prompt composition + reference loading
- `DIRECT` — already-resolved request; skips policy resolution entirely

`SPAWN_PREPARE` uses `LaunchArgvIntent.SPEC_ONLY` for execution paths; `LaunchArgvIntent.REQUIRED`
only for `ops/spawn/prepare.py` dry-run display (needs a real argv for `cli_command`).
Do not set `REQUIRED` on execution paths.

### Mars Launch-Bundle Policy Resolution (`policies.py`)

`PRIMARY` and `SPAWN_PREPARE` share `_resolve_policy_from_bundle()`. Mars resolves
the model, harness, and fallback chain; Meridian applies the returned launch bundle
rather than reconstructing profile policy composition or fallback ordering.

### _MERIDIAN_HARNESS Child Env

`bind_launch_context()` writes `_MERIDIAN_HARNESS = harness.id.value` into the child env.
It is registered in `ALLOWED_CHILD_ENV_KEYS`; each bind overwrites any inherited
value with the selected child harness before the complete environment crosses the
connection boundary.

The orchestrator reads `os.getenv("_MERIDIAN_HARNESS")` at wait time to determine
its own yield interval — it is asking about *its own* harness's prompt-cache TTL,
not the spawns it is waiting on.


### Agent Inventory Prompt

Agent inventory is bundle-only. Mars renders harness-aware inventory in the
launch-bundle `prompt_surface.inventory_prompt` field (Meridian spawn commands,
native-agent sections, model metadata). Meridian passes that string through
verbatim into the composed system prompt — no Python fallback renderer.

`model_invocable` filtering happens in mars at inventory render time. Catalog
scanning (`scan_agent_profiles()`, `load_agent_profile()`) stays neutral — CLI
listing and explicit `-a <name>` resolution are unaffected.

Resume and snapshot replay carry `bundle_inventory_prompt` on
`LaunchPolicySnapshot` so inventory survives without re-calling mars.

### Spawn CWD and Directory Contracts

`cwd.py` owns the three-directory model that governs where spawned agents work,
where references resolve, and where the child process actually runs:

```
authority_root          ← profiles, skills, config, KB authority
logical_task_cwd        ← where the spawned agent works; relative -f resolves here
actual_process_cwd      ← where the child process starts (may differ)
```

**`authority_root`** is the project/config root — profiles, skills, config, and
KB references all resolve relative to this. It never changes based on work item
selection.

**`logical_task_cwd`** (also called `reference_anchor`) is where the spawned agent
is expected to work. When a work item has a configured task directory, this is the
task directory. Relative `-f` paths resolve from here. Set by `resolve_task_cwd()`
from explicit task-dir/work-item intent, ambient work attachment, or the caller cwd
when a nested spawn is launched from outside the project tree.

**`actual_process_cwd`** is where the child process actually starts. It defaults
to `authority_root` (the project/control root). Harness subprocess cwd always
stays at control root; distinct task directories are exposed via `MERIDIAN_TASK_DIR`,
bind-time env, and task-cwd instructions — not by starting the process in the
task directory.

**Task CWD instruction injection.** For non-primary child spawns with a distinct
`logical_task_cwd`, launch composition adds a prompt instruction that states the
absolute `MERIDIAN_TASK_DIR`, states that the shell cwd is the project root (not
the task directory), and tells the agent to `cd` into `MERIDIAN_TASK_DIR` or use
absolute paths under it for reads, edits, git, builds, and commands. This is a
conservative child-spawn safety contract: harnesses may bind tools differently
than Meridian's requested process cwd. Primary sessions stay quiet unless a
future primary flow explicitly needs task-cwd steering. Controlled by
`LaunchDirectoryContext.should_inject_task_cwd_instruction(surface)`.

Resolution priority in `resolve_task_cwd()`:
1. explicit task-dir override
2. explicit work item task_dir
3. inherited task-dir (`MERIDIAN_TASK_DIR`)
4. ambient work item task_dir
5. caller cwd when outside the project tree (`source="ambient-cwd"`)
6. authority root default

**Stale task_dir fallback.** When a work item's configured `task_dir` no longer
exists on disk (the common case after a worktree is removed post-merge),
resolution skips the stale tier with a warning and falls through to the next
valid tier. The fallback is read-time only — work state is never mutated on a
read path. `WorkTaskDirMissing` is raised only when even the fallback
`authority_root` does not exist; it is mapped to error name
`work_task_dir_missing` at the pre-init boundary in `ops/spawn/pre_init.py`.
Both `resolve_task_cwd()` and `resolve_effective_task_dir()` implement this.

**Warning threading.** `TaskCwdResolution.warning` carries the stale-path
warning from `resolve_task_cwd()`. Callers must explicitly merge it into
`SpawnRequest.warning` because `launch_resolution.request_updates` does not
include it. Both `launch_primary()` in `__init__.py` and `build_create_payload()`
in `ops/spawn/prepare.py` perform this merge. Omitting the merge silently drops
the warning — a trap for any new caller of `resolve_launch_inputs()`.

### Reference Loading and Anchor Semantics

`reference.py` owns `-f` reference file loading and template substitution.

**Reference anchor** is `logical_task_cwd` — relative `-f` paths resolve from the
task working directory, not from `authority_root`. This means reference paths are
relative to where the agent works, not where config lives.

**`kb:` prefix** resolves from the authority KB directory (`authority_root/.meridian/.../kb/`
or configured KB root). The path after `kb:` must be relative and must not escape
the KB root. Example: `kb:architecture/launch-system.md`.

**`@` prefix is unsupported** — attempting to use it raises a `ValueError` directing
the user to use `kb:` instead.

Directory references (`-f <dir>`) render as a tree structure with blocked directories
(`.git`, `node_modules`, `.meridian`, etc.) and blocked suffixes (`.pyc`, `.egg-info`)
excluded. File references include content inline if under 100KB; larger or binary
files produce a warning instead.

### Work Item Task Directories

Launch uses a work item's `task_dir` as its logical task cwd. Persisted
`WorktreeMetadata` is state/display metadata only; launch does not provision or
manage git worktrees.

**Premature-derivation trap in `ops/spawn/prepare.py`.** `build_create_payload()`
derives the parent's inheritable task dir via `derive_inheritable_task_dir()`
before calling `resolve_launch_inputs()`. That derivation calls
`resolve_effective_task_dir()`, which can raise on a stale work-item path.
When an explicit `--task-dir` flag is present, `build_create_payload()` skips
the derivation entirely (sets `inherited_for_child = None`) so the stale value
never blocks an explicit override. Without this skip, the stale-path exception
fires before the explicit flag reaches `resolve_task_cwd()`, breaking the
documented config-precedence rule that CLI flags always win.

### Workspace Projection

`workspace_projection.py` owns `project_workspace_roots()` and
`OPENCODE_CONFIG_CONTENT_ENV`. Keeping this dependency-light module in `launch/`
prevents `harness` bootstrap from importing back into a partially initialized
harness package. It depends only on `core.types`, never on harness internals;
`harness/connections/opencode_http.py` imports the env constant from here.

Two steps inside `bind_launch_context()`:

1. **Gate**: `resolve_workspace_snapshot_for_launch()` raises on `"invalid"` workspace.
   `"none"` and `"present"` pass through.
2. **Projection**: `project_workspace_roots()` maps roots per harness — Claude/Codex get
   `--add-dir` args; OpenCode gets `OPENCODE_CONFIG_CONTENT` env override (deep-merged).

Roots include workspace roots, git context clone roots, context projection roots
(work, kb, extras), task cwd when task cwd is external, runtime root, and system
temp dir — deduplicated in order. Extra args remain user-owned passthrough; they
are not a workspace projection channel.

**Task CWD projection gap.** When `task_cwd` is outside both `control_root` and
all projected workspace roots, launch composition projects the exact task cwd
for active projection harnesses. If workspace projection is not active, a
`task_cwd_not_projected` warning is emitted. The launch continues but the agent
doesn't automatically get access to the task directory — harness-specific
configuration would be needed.

### Logging Convention

`launch/` uses `structlog.get_logger()`. Do not use stdlib `logging` here —
the split matters because `capture_library_diagnostics()` (wrapping `build_launch_context()`)
captures stdlib warnings during spawn; structlog bypasses it and would leak to stderr.

### `session_scope()` Teardown Contract

`session_scope()` in `session_scope.py` is the context manager for session lifetime.
Its `finally` block has a two-phase teardown — both phases always run:

```
finally:
  try:
    stop_session(runtime_root, resolved_chat_id)      ← marks session record stopped
  finally:
    reclaim_session_owned_scopes_for_chat(...)         ← terminates session_owned process scopes
```

The inner try/finally ensures scope reclamation runs even if `stop_session` raises.
`reclaim_session_owned_scopes_for_chat` terminates any process scopes the session owns
(e.g., managed primary backends) that were not released during normal teardown.

**Callers must not add manual scope cleanup after `session_scope`** — reclamation is
guaranteed by the context manager. Adding cleanup outside it creates a race between the
`finally` block and the caller's explicit cleanup.

The `_reclaim_session_scopes` parameter accepts a `Callable[[Path, str], object]` for
test injection; in production it always points to `reclaim_session_owned_scopes_for_chat`.

### User-Turn Context Threading

`resolve_task_context_inputs()` in `context.py` is the single seam for assembling
user-turn context blocks from `--from` refs and `-f` reference files:

The full four-mode session-initiation model and its rationale live at
`$MERIDIAN_CONTEXT_KB_DIR/concepts/session-initiation.md` (see `meridian context kb`).
This code-local note records only the launch seam and the fields it threads.

```python
@dataclass(frozen=True)
class TaskContextInputs:
    reference_items: tuple[ReferenceItem, ...]
    prior_output: str
    resolved_context_from: tuple[str, ...]

def resolve_task_context_inputs(
    *,
    context_from: tuple[str, ...],
    reference_files: tuple[str, ...],
    project_root: Path,
) -> TaskContextInputs: ...
```

Both `_resolve_spawn_prepare_projection()` and `_resolve_primary_projection()` call
this function so both launch paths receive the same user-turn context inputs.

**Content ordering** inside the assembled user turn:

```
1. -f reference blocks   (reference_items, rendered as context_blocks)
2. --from prior-context  (render_context_refs + sanitize_prior_output)
3. -p / --prompt-file    (current_request text)
```

**`LaunchRequest.context_from`** carries the resolved set of prior-context refs
(from `--from` on both spawn and primary surfaces). It is populated by the CLI layer
from `ForkModeResolution.resolved_context_from` and passed through to
`SpawnRequest.context_from` via `build_primary_spawn_request()` in `plan.py`.

**Do not unify `_resolve_spawn_prepare_projection()` and `_resolve_primary_projection()`.**
Only the user-turn context resolution step is shared. The two projections differ in
supplemental documents, agent profile body handling, report instruction, completion
contract, session seeding, and passthrough arg normalization. `resolve_task_context_inputs()`
is the only shared extraction between them.

#### Why User-Turn, Not System Prompt

Prior context is rendered into the user turn, not the system prompt. The full
rationale lives at `$MERIDIAN_CONTEXT_KB_DIR/concepts/session-initiation.md`;
at this layer, the rule is simply to keep `--from` blocks in user-turn context
blocks and out of `SystemInstruction`.

### Skill Loading — Bundle as Sole Interface

Skill content comes from the Mars launch-bundle JSON (`skills.loaded[]`), not from
reading `.mars/skills/` on disk. Mars owns the `.mars/` directory schema, skill
variant resolution, and compilation. Meridian consumes the structured output:

```
Mars launch-bundle → skills.loaded[{name, skill_type, body}]
                          │
                          ▼
_build_resolved_skills_from_bundle()  ← policies.py
                          │
                          ▼
ResolvedSkills.loaded_skills: tuple[SkillContent, ...]
```

`_build_resolved_skills_from_bundle` in `policies.py` constructs `SkillContent`
objects with synthetic paths (`project_root/.mars/skills/{name}/SKILL.md`) for
downstream heading rendering and snapshot persistence. The parser reads the
`body` field from each `skills.loaded[]` entry (mars >= 0.8).

**`resolve_skills_from_profile()` still exists** for the snapshot replay path
(`policy_snapshot.py`) — when replaying from a persisted snapshot whose
`loaded_skills` field is empty (pre-bundle snapshots), it falls back to disk
loading. New snapshots always persist `loaded_skills`, so this path is legacy compat.

### Skill Injection Channels

Two distinct channels deliver loaded skill content to agents. They are controlled
by separate capability flags and must not be conflated:

**Channel 1 — `supplemental_documents`** (prompt-embedded):
Populated by `compose_skill_prompt_documents()` in `_resolve_spawn_prepare_projection()`
and `_resolve_primary_projection()`. **Gated on `not harness.capabilities.supports_native_skills`.**
When the harness supports native skills (currently Claude, Codex, and OpenCode), this
channel is suppressed — `supplemental_documents` is set to an empty tuple.

**Channel 2 — `append-system-prompt`** (Claude spawns only):
Populated by `compose_skill_injections()` in `_prepare_spawn_surface()` when
`harness.run_prompt_policy().skill_injection_mode == "append-system-prompt"`.
This channel is **not** gated on `supports_native_skills` — it is preserved for
Claude spawn skill injection via `--append-system-prompt`.

### `prepare_prompt_payload` or-chain Pitfall

`prepare_prompt_payload(projected_content=X, appended_system_prompt=Y)` silently drops
`Y` when `X` is present — the implementation prefers projected content and the
or-chain short-circuits. Callers that need both (projected user-turn content from
`project_content()` **and** an appended system prompt for Claude skill injection) must
construct `PreparedPromptPayload` directly:

```python
PreparedPromptPayload(
    adhoc_agent_payload=...,
    appended_system_prompt=appended_system_prompt,  # not dropped
    user_turn_content=content.prompt_payload.user_turn_content,
)
```

`_prepare_spawn_surface()` uses direct construction for this reason. Avoid calling
`prepare_prompt_payload()` in any path where both projected content and an appended
system prompt must coexist.

## Patterns

**Never call `fork.materialize_fork()` before the spawn row exists.** Fork writes
a session artifact referencing the spawn — the row must exist first (I-10).

**Warnings go to `LaunchContext.warnings`, not stderr or logs.** Adapters that
surface composition issues through other channels violate I-13 and make warnings
invisible to callers.

**Background worker trusts the persisted `BackgroundWorkerLaunchRequest`.** It does
not re-resolve model or harness from the spawn record. The request is already fully
resolved when persisted. Empty `model` is accepted — model-optional profiles exist.

**Use `session_scope()` when managed backends need cleanup at session end.** The
context manager guarantees `reclaim_session_owned_scopes_for_chat()` runs even on
exception paths. Do not replicate this logic inline.

## Related KB

- `architecture/launch-system.md` — full adapter diagram, prepare/bind split detail, module map
- `concepts/session-initiation.md` — four-mode initiation semantics, user-turn placement, identity lock, bare-flag inference
- `decisions/launch.md#d-continue-replays-recorded-launch-contract-same-session-continue-is-not-live-policy-recomputation` — exact continue replay contract
- `concepts/composition-pipeline.md` — user-turn composition and harness projection details for `TASK_CONTEXT`
- `concepts/spawn-lifecycle.md` — spawn status machine, crash recovery, authority lattice
- `architecture/spawn-finalization.md` — finalization policy, per-spawn lock, `CompleteSpawnOutcome`

## Lateral Links

- `../../ops/.context/CONTEXT.md` — how `ops/spawn/` drives this layer
- `../../harness/` — adapters this layer calls into for `project_content()`, `preflight()`, `build_launch_argv()`
