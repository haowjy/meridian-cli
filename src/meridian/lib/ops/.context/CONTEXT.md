# ops/ — Operation Policy Layer

## Architecture

`ops/` sits between active surfaces (CLI commands and MCP tools) and mechanisms
(`launch/`, `state/`, `harness/`). It owns *what* to do, not *how* to run a process.

```
CLI / MCP
      │
      ▼
ops/          ← policy: access control, input validation, lifecycle sequencing
      │
      ├──→ launch/      ← composition + execution mechanism
      ├──→ state/       ← spawn/session event stores
      └──→ harness/     ← process adapters (indirectly through launch/)
```

### ops/spawn/ — The Spawn Policy Layer

The spawn subprocess is the second of three driving adapters into `lib/launch/`.
`ops/spawn/` owns foreground and background child-spawn paths (not primary sessions).

**SpawnApplicationService** (in `lib/bootstrap/services.py`, built by `build_spawn_application_service()`)
is the policy coordinator that `ops/spawn/api.py` instantiates. It sits above
`SpawnLifecycleService` (sole state writer) and below the driving adapters:

```
Layer 3: build_launch_context()      ← pure resolution, may fail, no side effects
Layer 4: SpawnApplicationService     ← lifecycle policy
Layer 5: SpawnLifecycleService       ← sole state writer (spawn_store)
```

Key `SpawnApplicationService` methods:
- `prepare_spawn()` — resolve-before-persist entry point for streaming-serve
- `cancel()` — surface-neutral cancel pipeline; managed-primary, signal, finalizing races
- `complete_spawn()` — idempotent terminal seam; acquires per-spawn lock internally
- `archive()` — validates terminal state, emits exactly one `spawn.archived`

### Execution Paths in ops/spawn/execute.py

**Foreground:** `execute_spawn_blocking()` → creates spawn row → calls
`launch_prepared_spawn()` via `asyncio.run()` → `execute_with_streaming()`.

**Background:** `execute_spawn_background()` → creates spawn row → persists
`BackgroundWorkerLaunchRequest` to disk → detaches subprocess. Worker calls
`_execute_existing_spawn()` → `launch_prepared_spawn()`.

Both paths converge at `launch_prepared_spawn()`. This helper:
1. Resolves session continuation (if resuming or forking)
2. Materializes fork (only after spawn row exists — invariant I-10)
3. Builds child env overrides
4. Calls `build_launch_context()`
5. Calls `execute_with_streaming()`

`launch_prepared_spawn()` owns pre-run failure finalization via a broad `except`.
This is safe because `complete_spawn()` is idempotent.

### Resolve-Before-Persist Pattern

The spawn subprocess path creates the row *before* calling `build_launch_context()`.
This is a known gap — it differs from streaming-serve, which uses
resolve-before-persist (`build_launch_context()` first, row creation only on success).

For streaming-serve, `SpawnApplicationService.prepare_spawn()` enforces:
- **SEAM-1**: No spawn row on resolution failure
- **SEAM-2**: Row metadata always reflects resolved model/agent/harness
- **SEAM-3**: `ConnectionConfig.env_overrides` populated from `LaunchContext.env_overrides`

### ops/spawn/prepare.py — The Exception to SPEC_ONLY

`prepare.py` uses `LaunchCompositionSurface.SPAWN_PREPARE` and
`LaunchArgvIntent.REQUIRED`. This is the only execution path that needs a real
argv — it populates `cli_command` for dry-run display. All actual execution paths
use `SPEC_ONLY`. Do not set `REQUIRED` on execution paths.

### ops/init_ops.py — Init Orchestration

`init_ops.py` is the single entry point for all `meridian init --add` and
`meridian init --link` invocations. `run_init_flow()` sequences:

1. `config_init_sync()` — bootstrap `meridian.toml` (idempotent)
2. `mars init` — create `mars.toml` if absent
3. `mars add <sources>` — skipped when no sources requested
4. `mars link <target>` — once per resolved target
5. `maybe_set_primary_agent()` — write `primary.agent` to `meridian.toml` if the
   package declares one and the config field is currently unset

**`_run_mars_json()` helper** consolidates all mars subprocess invocations.
Takes `command: str` and `args: list[str]`, passes `--json`, returns parsed output.
`run_mars_add_json()` is a typed wrapper on top that also calls `_scan_mars_content()`.

**`_scan_mars_content()` uses filesystem scanning, not JSON model parsing.**
It discovers content by walking `.mars/` subdirectories — skills are stored as
directories (containing `SKILL.md`), agents as files, so the scan uses
`f.is_file() or f.is_dir()`. Returns `dict[str, list[str]]` keyed by content
type (e.g., `{"agents": [...], "skills": [...]}`). This avoids coupling Python
to the mars JSON report shape — new content types (hooks, MCP servers, etc.) are
automatically counted without Python model changes.

### session_target.py / session_transcript.py — Ordered Transcript Sources

Session-log target resolution builds an ordered source plan once, then parsing walks
that plan. Do not implement fallback by recursively re-entering
`resolve_session_log_target()` or by fabricating placeholder files for non-file
sources. `SessionLogTarget.sources` is the durable plan; each `TranscriptSource`
identifies its kind (`file`, `opencode_db`, or `spawn_history`), session id,
harness, label, and optional path.

OpenCode completed-session precedence is:

1. `opencode.db` when a matching `session.id` exists;
2. native transcript file (`storage/session_diff/...` / legacy JSON) when present;
3. Meridian spawn `history.jsonl` as fallback/debug/live output.

`parse_session_target()` tries sources in order and stops at the first source with
usable user/assistant interaction content. This preserves the completed-session
preference for OpenCode DB while still allowing native-file or spawn-history fallback
when a DB row exists but contains no conversation rows. Spawn history is a fallback
source, not the preferred completed OpenCode transcript.

### session_log_render.py — Session Log Rendering

Pure rendering module with no IO. Sits at the end of the session-read pipeline:

```
session_transcript.py  ← parses raw JSONL transcript
      │
session_log.py         ← windows entries, structures SessionLogOutput
      │
session_log_render.py  ← renders to string (clean/raw modes, tool collapsing)
```

Structured data flows through in full; truncation and collapsing happen at
render time inside `render_session_log`, not before.

**Content pipeline** within the module:

`clean_content(text)` strips harness XML wrappers while preserving unknown tags.
Handled tags: `<command-name/args/message>` blocks → `/command args`,
`<bash-input>` → `$ cmd`, `<bash-stdout/stderr>` blocks → raw output (stderr
prefixed with `stderr:`), `<local-command-stdout>` → ANSI-stripped text,
`<system-reminder>` / `<usage>` / `<local-command-caveat>` → removed,
`<system_notification>` → `[notification: status — summary]`,
`<user_query>` / `<persisted-output>` → unwrapped content,
`<tool_use_error>` → `[error: ...]`. ANSI escape sequences are stripped from
`local-command-stdout` only — other content is passed through unchanged.

`_truncate_preview(content)` limits to 80 lines / 8000 chars and appends
`...[truncated: omitted N lines, M chars; rerun with --no-truncate]`.

`render_entry(entry, *, clean, truncate) → (lines, collapsed)` formats one entry:
- `clean=False` (raw / verbosity > 0): emits `--- N [segment S · messages M-M] [role] ---` header with raw content.
- `clean=True` (default, verbosity ≤ 0): emits `---` separator and `**Role** [N]` markdown header. In truncate mode delegates to `_render_collapsed_tools()`; otherwise to `_render_expanded_tools()`.

`render_session_log(...)` assembles the full output: header, per-entry blocks,
navigation commands (`Previous:`, `Next:`), and hints. Appends
`"Use --no-truncate to expand tool outputs"` when any entry collapsed tool output.

**Tool collapsing** (`_render_collapsed_tools`): when `truncate=True`, tool
invocations collapse to one-liners using the typed `ToolCall` from
`SessionLogEntryMessage`:
- Bash: `  $ cmd`
- File tools (`read`, `write`, `edit`, `grep`): `  Read path`, `  Write path`, etc.
- stdin: `  (stdin)`
- Other: `  name: detail`

Exit failures for bash are shown inline: `  (failed: exit N)`. Tool results are
suppressed in collapsed mode — only failures surface. `_render_expanded_tools`
shows full tool output indented with 2 spaces.

**`ToolCall` threading**: `SessionLogEntryMessage.tool_call` and `.is_tool_result`
are sourced from `AbsoluteTranscriptMessage` (threaded in `_entry_message_row()`
in `session_log.py`), which in turn gets them from `harness/transcript.py`. The
render layer works from these typed fields — it never re-parses content strings
to detect tool boundaries.

**Render mode selection**: `SessionLogOutput.format_text(ctx)` passes
`ctx.verbosity` to `render_session_log`, which derives `clean = verbosity <= 0`.
The verbosity level is the single control point — callers don't set `clean`
directly.

**Protocols**: `SessionLogRenderableMessage` and `SessionLogRenderableEntry` are
structural `Protocol` types. `SessionLogEntryMessage` and `SessionLogEntry`
satisfy them. The render functions accept the protocols, not the concrete models,
keeping the render layer decoupled from the data layer.

## Contracts

### OperationRuntime

`runtime.py` provides `OperationRuntime` and `build_runtime()` — the ops-layer
equivalent of `LaunchRuntime`. It resolves runtime root from env (`_MERIDIAN_RUNTIME_DIR`),
project state, or user home. Operations that need both project and runtime state
use `resolve_runtime_root_and_config()`.

### Depth Guard

`ops/spawn/api.py` checks `max_depth_reached()` before executing spawns. A spawn
inside a spawn inside a spawn eventually hits the depth limit; the outer caller
gets `depth_exceeded_output()` instead of a new spawn. The reaper also checks
`_MERIDIAN_DEPTH` and skips reaping when inside a spawn.

### Session Reference Resolution

`ops/reference.py` exposes `resolve_session_reference()` → `ResolvedSessionReference`.
This resolves spawn IDs (e.g., `p123`), chat IDs, and bare references to canonical
spawn rows. Operations that accept `--from` or `-f` go through this.

Session browse has a narrower authority question: whether a primary chat has a
durably recorded harness session id and may therefore resume or fork.
`session_list.py` uses the batch
`recover_recorded_chat_harness_session_ids()` and `session_reentry.py` uses its
single-chat counterpart from `reference_recovery.py`. Both check the session
store, primary spawn row, and `primary_meta.json`, but never perform native
transcript discovery. The batch form must keep one spawn-state scan for the
whole listing; a scan per missing session makes startup scale as sessions ×
spawns. Listing carries an advisory action for the UI; Enter re-reads the same
durable authorities plus lease liveness. Using the general resolver in only one
path can make the displayed verb disagree with the action even when no lease
changed.

### Logging Convention

`ops/` uses `structlog.get_logger()` throughout. Do not use stdlib `logging` —
the logging split between catalog/config (stdlib) and ops/launch/harness (structlog)
matters for the `capture_library_diagnostics()` boundary in `build_launch_context()`.

## Patterns

**ops/ is policy, not mechanism.** If you find yourself building argv, merging
envs, or projecting workspace roots inside ops/, that logic belongs in `launch/`.

**`commands.py` is the operation manifest.** CLI and MCP surfaces discover
available operations through this module. Adding a new operation requires an entry
here; otherwise it is invisible to those surfaces.

**Work item attachment is ops-layer responsibility.** `work_attachment.py:ensure_explicit_work_item()`
handles `--work` resolution before the spawn row is created. The resumed-session
case (reading `preserved_work_id`) is handled inside `run_harness_process()` in
`launch/process/`.

**Sync/conflict boundary: ops resolves roots, autosync_store owns artifacts.**
`sync_conflicts.py` and `context.py` follow the same pattern: ops determines
*which* directories are sync roots (context resolution via `_find_sync_roots()`
and `resolve_context_paths()`), then delegates all artifact access to
`hooks/builtin/autosync_store`. The store is the single owner of
`.meridian/autosync/` path layout and JSON parsing — no other module
constructs those paths directly. `_find_sync_roots()` in `sync_conflicts.py`
uses `autosync_store.has_autosync_state()` to filter candidates, so candidate
discovery stays in ops and artifact presence checking stays in the store.

## Related KB

- `architecture/launch-system.md` — launch adapter architecture; ops/spawn is adapter #2 of three
- `concepts/spawn-lifecycle.md` — spawn status machine ops surfaces expose
- `architecture/spawn-finalization.md` — `SpawnApplicationService.complete_spawn()`,
  `CompleteSpawnOutcome`, per-spawn lock

## Lateral Links

- `../../launch/.context/CONTEXT.md` — mechanism layer ops/spawn drives
- `../../spawn/.context/CONTEXT.md` — archive visibility ops/spawn reads
