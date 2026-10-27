# launch/ — Composition and Execution

## Architecture

### The Prepare/Bind Composition Seam (Invariant I-1)

Runtime launch composition follows the prepare/bind pipeline in `context.py`.
`prepare_launch_surface()` produces the mandatory `PreparedLaunchSurface`, then
`bind_launch_context()` produces the complete argv, environment, cwd, and
permissions consumed by the driving adapter and its connection. No adapter
recomposes the child environment from `os.environ`.

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
LaunchContext                      ← pure bound plan
        │
        ▼
materialize_launch_context()       ← real-launch-only adapter writes
```

**Why the split?** The primary CLI path needs to call `bind_launch_context()` twice:
once with `dry_run=True` for `--dry-run` display, then again with real spawn-ID and
paths for actual execution. `prepare_launch_surface()` is expensive (mars calls, profile
loading, prompt assembly) and safe to do once before the session is opened.
`bind_launch_context()` is write-free and repeatable; ignoring an active direnv environment invokes
a bounded subprocess on each bind, including previews.
The process runner, CLI spawn runner, and streaming spawn service all send their real bound
context through `materialize_launch_context()`; adapters therefore have one owner for launch-time
writes, and dry-run binds never materialize them.

### PreparedLaunchSurface

The in-memory boundary between preparation and binding. Carries: resolved request,
harness, composition warnings, loaded references, agent inventory prompt, capability
guidance, alias catalog, model selection context. Deliberately excludes spawn IDs,
report paths, env, argv, and permission outputs — everything that varies per bind.

`launch_request` on `PreparedLaunchSurface` preserves the original pre-resolution
request for `LaunchContext.request` compatibility. `request` carries the resolved
version used by `bind_launch_context()`.

Every bind replaces the inherited caller boundary with one record for the process it is
about to start. It contains the launcher's PID namespace, resolved sandbox mode,
the prepared profile's spawn capability, and the Meridian-projected typed network policy for every
constrained sandbox. A missing network key means no network floor, preserving boundaries
written by older builds. The projection is a pure function of Meridian's resolved sandbox,
network, and native override. Harness-owned user-config detection runs after the final child
environment and cwd are bound, but contributes only attributed warnings. The child
guardrail and permission report ignore those settings and use the same pure projection.
Projected roots are not part of this CLI-escalation guardrail.

### RuntimeBindings

Frozen dataclass for runtime-only values: `spawn_id`,
`runtime_work_id`, `chat_id`, `forked_harness_session_id`, `plan_overrides`, `dry_run`.
These are the only things that differ between the preview bind and the real bind.
The report and `system-prompt.md` paths are derived in `bind_launch_context` from
`spawn_id` (the spawn log dir), not carried here.

### Native identity order

All runners use `bind_entry` before exec and `NativeRun.observe` for owned
signals. Only a first signal contradicting pre-exec assignment or reusing a fork
source fails the attempt. Later signals and the connection's current ID are
store-authoritative diagnostics. `conclude_native_run` runs after teardown joins:
artifact first ID, connection current ID, `observe_after_exit`, entry verification,
exit allocation, one boundary write, typed failure or invocation attribution.
Repeated delivery of the same ID within an attempt binds/logs once.
New exit chats are allocated only when the exact native resolver finds a valid
session; an already-bound exact chat still wins, including the entry chat. A verified
exit chat is the post-run target for spawn references and resume hints; chat references
remain immutable, and unresolved or mismatched runs target their entry chat.
Invocation attribution waits for validation. Startup refusals
outside runner finalization flow through `ops/spawn/failure_policy`.

Claude fullscreen correlation is diagnostic, not owned exit evidence. Only
Pi's correlated final quit currently supplies a verified exit. A tracked read
requires the complete recorded native key; no legacy hint repair is allowed.

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

Continuation revalidates its model and pinned harness through Mars under current
target/caller restrictions. Explicit model input resolves normally; otherwise the
recorded selection wins. Recorded canonical IDs are literal pins, including deliberate
harness-default mode. A new invocation snapshot updates routing fields without
rewriting the source snapshot.

Replay preserves non-routing policy: agent/profile, skills and loaded content, the
available-skills listing (names, descriptions and paths, never bodies), execution
policy, `tool_rules` and `platform_tool_rules`, terminal surface, inventory prompt, env
and passthrough args. Legacy `mcp_tools` is accepted when reading a snapshot and ignored
because it contains permission names, not MCP configuration. Exact continue permits an
explicit model override with a cache/cost warning; it still rejects agent, skills,
execution-policy, passthrough, env, work and task-dir mutations. Agent opt-out is also a
mutation.

The system prompt is not stored separately: an exact continue recomposes it from the
snapshot through the same prepare pipeline. Because every per-launch value lives in the
user turn, identical snapshot inputs produce identical bytes within one Meridian
version (a package upgrade may change the rendered guidance), which is what keeps Pi's
and OpenCode's prompt cache warm. A real identity change on continue — a new goal, a
primary-vs-spawn surface change — correctly changes the prompt instead of being hidden
by a replay. Foreground and background spawns share one preamble, so continuing in the
other mode is not an identity change. A snapshot an older build wrote lacks the
available-skills field and recomposes without that listing.

Claude is the exception in mechanism, not outcome: its `--system-prompt-snapshot`
default is `on`, so the CLI records the prompt on the conversation's first request and
reuses that record on every later request and resume, even when a later launch passes
different text, until the conversation is compacted. Meridian still writes and passes
the recorded prompt file; Claude discards it. Editing an agent profile or launch config
therefore does not reach a continued Claude conversation until it compacts.

Primary and spawn accepted-running callbacks append selection intent through
`SessionAttempt`. Its captured generation/startup attempt also binds native-ID
observations. A recording failure is a coordination error, not a reason to replay
the model turn. The record means accepted selection, not
model execution. Legacy baseline lookup reads the original session generation;
the session scope seeds that value under the existing store lock before startup.
Previews do not seed. Missing snapshots retain partial historical values; a
tracked conversation with neither accepted selection nor original legacy history
requires an explicit model rather than adopting a failed attempt's snapshot.
Streaming-serve recording remains unimplemented.

Primary adapters declare named-model resume support. Unsupported named resumes fail
before native startup; a named selection never falls back to an empty model.

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
| I-1 | All runtime composition uses `prepare_launch_surface()` then `bind_launch_context()`; adapters do not compose independently |
| I-2 | Driving adapters and connections consume the bound argv, env, and permissions without reconstruction |
| I-4 | `conclude_native_run()` once per turn after teardown joins: IDs → adapter → boundary → attribution |
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
the model and harness; Meridian requires bundle schema 4 with report schema 2 and
applies the returned launch bundle rather than reconstructing policy or fallback
ordering. `selection_report` preserves Mars's `routing.route_trace` in snapshots,
previews, and failure outputs as diagnostic history, never an executable retry queue.
The adapter checks report pointers and selected identity against executable routing.
Structured Mars errors retain their code and available report across pre-init failure.
A schema-4 bundle without `provenance.network_source` came from Mars < 0.18 and is a hard
composition error that names the selected executable and tells the user to upgrade
Meridian. At this boundary, current
`execution_policy.network` must be the Mars object `{mode, allow, deny}`; it is parsed once
into `Allow(deny)`, `Hosts(allow, deny)`, or a typed sandbox-derived value. A null mode is
resolved only after Meridian applies final sandbox precedence (including
`primary.sandbox`), retaining denies and warning when a constrained sandbox drops allow
hosts. The separate durable-policy parser continues to accept old scalar snapshots. Raw
launch `NetworkOverrides` ride `BundleRequest` to Mars so its normal profile/overlay/CLI
merge remains the only precedence implementation.

### _MERIDIAN_HARNESS Child Env

`bind_launch_context()` writes `_MERIDIAN_HARNESS = harness.id.value` into the child env.
It is registered in `ALLOWED_CHILD_ENV_KEYS`; each bind overwrites any inherited
value with the selected child harness before the complete environment crosses the
connection boundary.

The orchestrator reads `os.getenv("_MERIDIAN_HARNESS")` at wait time to determine
its own yield interval — it is asking about *its own* harness's prompt-cache TTL,
not the spawns it is waiting on.

### Direnv Inheritance

`environment.ignore_direnv` is resolved from the config snapshot unless the launch
request explicitly overrides it. Bind reverses direnv's recorded diff before
deriving environment overlays (including OpenCode workspace JSON). Adapter defaults
and executable probes consume the effective environment, never ambient `os.environ`.
Bind emits the resolved `MERIDIAN_IGNORE_DIRENV` into the
child environment. Descendants retain the decision even if direnv supplied its
original env input. `env.py:undo_direnv` uses direnv's own reversal, with a bounded
subprocess only when `DIRENV_DIFF` exists; captured environment and stderr must
never appear in errors or artifacts. This does not prevent tools reloading `.env`.

### Harness Profile Env Projection at Bind

Config-resolved harness profile values that the child must observe are baked into
`child_context_env` during `bind_launch_context()`, not inherited from the
ambient environment. For OpenCode, the resolved
`[harness.opencode].version` preference (from the launch config snapshot, with a
project-config fallback) is written to `MERIDIAN_HARNESS_OPENCODE_VERSION`.
`resolve_opencode_version_for_launch()` owns the snapshot/env precedence; the key
is registered child-injectable in `env_registry.py`. Right after `build_env_plan()`,
bind calls `bind_launch_spec()` (the harness's `bind_spec` port), which resolves the
version against that env once and stores it on the spec; the report, the env and argv
projections, the preview and the connection all read the spec, so preview and
execution select the same backend. Capture agrees through schema detection, not the
env var.

The same step lets the harness project config into the child env: after
`build_env_plan()`, bind calls `harness.project_launch_env()` with the spec, the
`system-prompt.md` path and the bound env, and merges the returned overrides into
the final env. Adapters use this for forced launch policy: Claude's autocompact
and spawned-cache overrides, Pi's runtime policy, and OpenCode's prompt, model
and workspace grants (see the
harness [OpenCode prompt delivery](../../harness/.context/CONTEXT.md#opencode-prompt-agent-and-model-delivery)).

### Agent Inventory Prompt

Agent inventory is bundle-only. Mars renders harness-aware inventory in the
launch-bundle `prompt_surface.inventory_prompt` field (Meridian spawn commands,
native-agent sections, model metadata). Meridian passes that content through verbatim;
producer guidance belongs to Mars rather than string-matched patches in the consumer.
There is no Python fallback inventory renderer.

`model_invocable` filtering happens in mars at inventory render time. Catalog
scanning (`scan_agent_profiles()`, `load_agent_profile()`) stays neutral — CLI
listing and explicit `-a <name>` resolution are unaffected.

Resume and snapshot replay carry `bundle_inventory_prompt` on
`LaunchPolicySnapshot` so inventory survives without re-calling mars. The same
snapshot carries `available_skills` (names, descriptions, paths) so the
`# Available Skills` listing recomposes instead of disappearing on continue.

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
bind-time env, and capability guidance — not by starting the process in the
task directory.

**Directory capability guidance.** Every primary and spawn prompt has one
`# Meridian capabilities` section. Preparation leaves one typed placeholder;
`bind_launch_context()` renders the whole block once, after the profile's spawn
capability and declared mode and the final control root, logical task cwd, work
item, chat and KB are known. Every execution has its own chat and therefore its own
two links: the work directory (`work_link`, a literal path with no item ID, because
the ID goes stale after `work switch`, and no scratch path, because `work done` moves
it) and the source checkout (`task_link`, which replaces the concrete directory line
and says to write the literal path, because `task-dir`, `work task-dir`, and a switch
repoint it). Both are projections, not authority: `launch/session_links.py` reads the
chat row, the spawn scope and the work item under the sessions lock and swaps one
symlink per link, and the launch env names the same paths
(`MERIDIAN_ACTIVE_WORK_DIR`, `MERIDIAN_TASK_DIR`). The capability block carries a
primary's own chat ID and its two links, because a primary's `--continue` replays no
user turn; per-launch values are rendered into the first user turn
(`build_launch_facts_prompt()`), because one changed byte in the system prompt
invalidates the harness prompt cache for the whole conversation. That covers the run
ID, a spawn's launching chat, and a spawn's two links — their paths name the spawn's
own chat — plus a spawn's goal contract. Sibling spawns therefore send identical
system bytes.
A primary's `--goal` is a guidance block instead: it holds for the whole conversation,
and goal text must never create a user turn. A primary that launches bare would
otherwise submit the goal as its first message, fresh and on every resume, and a Codex
primary would lose it entirely because its user turn is synthetic. A continued
conversation inherits the goal from its row (`SpawnRecord.goal`) unless `--goal`
overrides it. The work item's own goal is neither text nor user turn: the block
carries a pointer, "run `meridian work show`", and that command resolves the session's
current work item itself, so the prompt holds no goal that could be edited
mid-conversation and no ID that could go stale. The block names
`$MERIDIAN_ACTIVE_WORK_DIR` and `$MERIDIAN_TASK_DIR` for the links it names. Its run-ID row
names `$MERIDIAN_SPAWN_ID` and points at `meridian task-dir --json`, which prints it as
`spawn_id`: a restricted shell cannot expand the variable, and `meridian spawn list` lists runs
without saying which one is the caller's. The notification
row is present only for a `mode: primary` profile; delegation rows remain gated by
spawn capability. The source line names the actual control root as shell cwd and gives
the absolute task directory; when they match it collapses to one shorter line, and a
primary's checkout is always the second form because its link never equals the
control root.
Restricted shells need the resolved paths because they refuse variable expansion and
compound `cd` calls. Do not compose or append a second directory instruction before
or after projection.

Every prompt also has a short `# Meridian skills` block generated from the
descriptions under `meridian.skills`. It is a trigger index, not loaded skill
content: the agent reads a matching entry through `meridian skills`. A skill Mars
loaded with non-blank content into the prompt is omitted from the index. An empty
loaded entry has no prompt document, so its trigger remains. Bundled names use the
`name__meridian` collision spelling. The
`handoff__meridian`, `notify__meridian`, `work__meridian`, `escalation__meridian`,
and `prompt-authoring__meridian` triggers carry `metadata.mode: primary`;
`sessions__meridian` remains visible to every profile. The gate follows the profile's
declared mode even when a primary-mode profile is launched as a spawn, and comes from
skill frontmatter rather than a hardcoded name set. Package skills remain on the Mars/harness path below.

Spawn-capable prompts also receive `# Prompting subagents`: durable briefing
judgment that must remain in the prompt even though flag mechanics live in the
capability row and `meridian spawn -h`.

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

`workspace_projection.py` owns `project_workspace_roots()`, the per-harness
applicability that the config surface reports and bind's `task_cwd_not_projected`
check reads. Claude and Codex project roots as `--add-dir` args; OpenCode reports
`active` but projects nothing here: `OpenCodeAdapter.project_launch_env()` turns
`spec.projected_roots` into `external_directory` grants in the one config it builds.

Two steps inside `bind_launch_context()`:

1. **Gate**: `resolve_workspace_snapshot_for_launch()` raises on `"invalid"` workspace.
   `"none"` and `"present"` pass through.
2. **Projection**: `project_workspace_roots()` reports per-harness applicability for
   the roots (see above).

Roots include workspace roots, git context clone roots, context projection roots
(work, kb, extras), task cwd when task cwd is external, runtime root, and system
temp dir. Bind pre-creates the exact project lifetime lock; POSIX lock acquisition opens
an existing lock file read-only, so the shared sibling `.locks` directory is not projected.
POSIX launches also
project the private socket directory `/tmp/meridian-<uid>` so a sandboxed runner can bind
its control endpoint. Roots are deduplicated in order. Extra args remain user-owned
passthrough; they are not a workspace projection channel.

**Task CWD projection gap.** When `task_cwd` is outside both `control_root` and
all projected workspace roots, launch composition projects the exact task cwd
for active projection harnesses. If workspace projection is not active, a
`task_cwd_not_projected` warning is emitted. The launch continues but the agent
doesn't automatically get access to the task directory — harness-specific
configuration would be needed.

### Logging Convention

`launch/` uses `structlog.get_logger()`. Do not use stdlib `logging` here —
the split matters because `capture_library_diagnostics()` wraps spawn preparation
and captures stdlib warnings; structlog bypasses it and would leak to stderr.

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
After reclamation, a primary session makes one best-effort model observation
(`observe_session_model(..., best_effort=True)`): try-locks only, history tail only, exceptions
suppressed. It never delays reclamation, and a contended lock just skips the observation.
`reclaim_session_owned_scopes_for_chat` terminates any process scopes the session owns
(e.g., managed primary backends) that were not released during normal teardown.

**Callers must not add manual scope cleanup after `session_scope`** — reclamation is
guaranteed by the context manager. Adding cleanup outside it creates a race between the
`finally` block and the caller's explicit cleanup.

The `_reclaim_session_scopes` parameter accepts a `Callable[[Path, str], object]` for
test injection; in production it always points to `reclaim_session_owned_scopes_for_chat`.

### User-Turn Context Threading

`--from` is resolved once, at the request boundary, into `ContextRef`s:
`cli/primary_launch.py` for primary launches and `ops/spawn/prepare.py` for
spawns (both through `ops/spawn/context_ref.resolve_context_ref`). The request
carries only `context_refs`. Launch never resolves a ref; it renders them with
`core/context_ref.render_context_refs`.

`resolve_task_context_inputs()` in `context.py` is the single seam that turns
`context_refs` and `-f` reference files into user-turn context blocks:

The full four-mode session-initiation model and its rationale live at
`$MERIDIAN_CONTEXT_KB_DIR/concepts/session-initiation.md` (see `meridian context kb`).
This code-local note records only the launch seam and the fields it threads.

```python
@dataclass(frozen=True)
class TaskContextInputs:
    reference_items: tuple[ReferenceItem, ...]
    prior_output: str

def resolve_task_context_inputs(
    *,
    context_refs: tuple[ContextRef, ...],
    reference_files: tuple[str, ...],
    authority_root: Path,
    reference_anchor: Path,
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

**`SpawnRequest.prompt` is always the caller's raw prompt.** Every prepare composes
from the request's inputs (`prompt`, `context_refs`, `reference_files`,
`template_vars`); the composed text lives on the output side
(`PreparedLaunchContent.final_prompt`, `prompt_payload.user_turn_content`, and
after bind `binding.run_params.prompt`). The background worker and a session
change in `execute_runner` both prepare the persisted request again, so writing
the composed text back into `prompt` renders prior context and references twice.
Values for metadata come from `context_ref_values(request.context_refs)`; nothing
keeps a second string copy of the refs.

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

### Skill Loading — Mars Bundle Interface

Skill content comes from the Mars launch-bundle JSON (parsed in
`bundle_adapter.py`), not from Meridian reading `.mars/skills/` on disk. This includes
bundled Meridian skills Mars provided when a profile declares them. Mars reads the agent profile's `skills:` (a flat list
means all `load`; a mapping has `load` and `available`), adds the `--skills`
names Meridian passes as `--skill` to `load`, selects the harness variant, and
returns:

```
skills.loaded[{name, skill_type, source_path, body}]   → _build_resolved_skills_from_bundle()
                                                          → ResolvedSkills.loaded_skills
skills.available[{name, skill_type, description, source_path}] → available listing (names only)
skills.missing[name]                                   → warning, not a failed launch
```

Neither list is filtered by `model-invocable`. A loaded skill is composed in full
whatever its value; `model-invocable` only decides whether the harness's own
skill list shows it, and that list is not how loaded skills arrive. Don't add an
invocability check on this path. The skill block header (`# Skill: /<name>`,
`render_skill_block()` in `prompt_context.py`) tells the agent the skill is
already loaded, because agents otherwise look for it in the harness list, miss
it, and skip it. `meridian skills <name>` renders bundled skills through the same
function, so reading one is indistinguishable from activating it.

`SkillContent.path` is the bundle's `source_path` (the base `SKILL.md`, even when
a variant body was selected); a reconstructed primary-root path is only the
fallback for producers that omit it. The header's resource directory comes from
this path.

Snapshot replay consumes only the `loaded_skills` embedded in the snapshot. An empty
field replays with no loaded skills; Meridian never re-resolves a snapshot against
the current filesystem.

### Loaded Skills Are Composition Documents

Loaded skills reach the agent one way on every harness, primary and spawn:
`compose_skill_prompt_documents()` turns them into supplemental documents in
`_resolve_spawn_prepare_projection()` and `_resolve_primary_projection()`, and
harness projection places them in the system prompt after the agent profile.
Bind-time re-projection rebuilds from the same composed content, so the skills
survive it without special handling. An exact continue takes them from the launch
snapshot, including the on-demand `available_skills` listing (whose bodies the
snapshot never stores), so the recomposed system prompt matches the conversation's.

`skill_delivery` on the launch policy chooses what each document holds. `inline`
(the default) writes the skill's body, headed with its name and directory. `named`,
the experimental `[experimental] skill_delivery` setting, writes one line naming the
file to read and the skill's `/name` instead — the file-read fallback is what keeps
a `model-invocable: false` skill reachable, since no harness's skill tool lists it.
The renderer never sees the mode: it groups documents by type and heads each group
in both modes, so named delivery keeps the same headers and can only differ inside a
skill's block. `# Available Skills` is unchanged in both modes. The mode is part of
the persisted launch-policy snapshot, so an exact continue renders as its source
launch did even when config or the environment changed; a snapshot written before
the field existed replays as `inline`. Named mode points at the base `SKILL.md`, so
on a harness with a `variants/<harness>/SKILL.md` it names the base file rather
than the variant body Mars delivered.

Don't add a second channel keyed on a harness capability. A native skill list
shows only model-invocable skills, so a loaded skill that isn't model-invocable
reaches the agent only through composition. Claude reads the appended prompt from
`system-prompt.md` (`--append-system-prompt-file`), which `write_projection_artifacts()`
writes from the full appended prompt. Capability guidance and loaded skills have
already passed through the same harness projection by then.

Delivery is the harness's job. OpenCode's happens in bind, where `project_launch_env()`
puts the appended prompt in the child env as a config agent reading `system-prompt.md`
(see the harness [OpenCode prompt delivery](../../harness/.context/CONTEXT.md#opencode-prompt-agent-and-model-delivery)),
so loaded skills reach the agent with the rest of the prompt. Don't add a skill channel here.

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
- `../../harness/` — adapters this layer calls into for `project_content()`, `bind_spec()`, `build_launch_argv()`

### Dry-run model resolution

Primary/launch preparation forwards dry-run to the existing Mars bundle and
static alias-list `--no-refresh-models` options, including snapshot replay.
OpenCode catalog-only routing must not run native availability
probes or materialize a managed backend/session just to describe a launch. Normal
execution retains its live availability behavior. This does not promise a wholly
write-free CLI: telemetry/root/static-cache setup may still write local files.
OpenCode inherited config must remain a JSON object through workspace projection;
malformed input is rejected, not silently replaced with an empty configuration
before adapter validation.

OpenCode permissions are compiled in `harness/projections/permissions_opencode.py`
and projected with workspace grants into the reserved config agent by
`project_opencode_config.py`. `OpenCodeAdapter.project_launch_env()` writes
`OPENCODE_CONFIG_CONTENT` at bind for both versions; transports consume it unchanged.
The ordered policy precedes appended workspace grants, and JSON serialization must
preserve rule order. See [harness context](../../harness/.context/CONTEXT.md).
