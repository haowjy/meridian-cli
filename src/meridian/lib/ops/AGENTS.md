# lib/ops/ — Operation Policy Layer

Policy between surfaces and mechanisms. CLI commands and MCP tools call into ops;
ops calls into `launch/`, `state/`, and `harness/`. Owns *what* to do — access
control, input validation, lifecycle sequencing — not *how* to run a process.

```
CLI / MCP
      │
      ▼
ops/          ← policy: depth checks, work attachment, validate, route
      │
      ├──→ launch/     ← composition + execution
      ├──→ state/      ← spawn/session stores
      └──→ harness/    ← adapters (indirectly through launch/)
```

If you find yourself building argv, merging envs, or projecting workspace roots
inside ops/, that logic belongs in `launch/`.

## ops/spawn/ — The Core Subpackage

The spawn subprocess is the second of three driving adapters into `lib/launch/`.
This package owns foreground and background child-spawn paths (not primary sessions).
→ [spawn/AGENTS.md](spawn/AGENTS.md)

**SpawnApplicationService** (built by `build_spawn_application_service()` in
`lib/bootstrap/services.py`) is the policy coordinator. Its key methods:

- `prepare_spawn()` — resolve-before-persist entry point for streaming-serve.
  No row created on resolution failure (SEAM-1).
- `cancel()` — surface-neutral cancel pipeline: managed-primary, signal, finalizing races.
- `complete_spawn()` — idempotent terminal seam; acquires per-spawn lock internally.
- `archive()` — validates terminal state, delegates to `lib/spawn/`.

**Depth guard:** `api.py` checks `max_depth_reached()` before executing spawns. A
spawn chain that hits the limit returns `depth_exceeded_output()` instead of a new
spawn. The reaper also reads `MERIDIAN_DEPTH` and skips reaping when nested.

## Operation Categories

**Session:** `session_transcript.py`, `session_log.py`, `session_log_render.py`
(pure rendering — clean/raw modes, tool collapsing, content pipeline),
`session_render.py`, `session_search.py`, `session_target.py`, `session_export.py`,
`session_repair.py`.

**Work and workspace:** `work_lifecycle.py`, `work_attachment.py`,
`work_dashboard.py`, `workspace.py`.

**Config and catalog:** `config.py`, `config_surface.py`, `catalog.py`, `mars.py`.

**Sync and conflicts:** `sync_conflicts.py` — CLI-facing conflict management
(`list_conflicts_sync`, `show_conflict_sync`, `resolve_conflict_sync`). Resolves
which directories are sync roots via context config; delegates all artifact
access (read, mark-resolved, AGENTS.md notice removal) to
`hooks/builtin/autosync_store`. Does not construct paths into `.meridian/autosync/`
or parse conflict JSON. `context.py:_sync_status_for_context()` calls
`autosync_store.read_status()` → `SyncRootStatus` to add sync summary lines to
`meridian context` output.

**Infrastructure:** `runtime.py` (`OperationRuntime`, `build_runtime()`),
`commands.py` (operation manifest), `hooks.py`, `diag.py`, `qi.py`,
`report.py`, `reference.py`, `context.py`, `migration.py`, `pruning.py`.

## Key Rules

**`commands.py` is the operation manifest.** CLI and MCP surfaces discover available
operations through this module. Adding a new operation requires an entry here —
otherwise it is invisible to both surfaces.

**Work item attachment is ops-layer responsibility.** `work_attachment.py:ensure_explicit_work_item()`
handles `--work` resolution before the spawn row is created.

**Use `structlog.get_logger()`.** Never use stdlib `logging` in ops/ — the logging
split matters for `capture_library_diagnostics()` at the launch boundary.

**Session references go through `ops/reference.py`.** `resolve_session_reference()`
→ `ResolvedSessionReference` handles spawn IDs (`p123`), chat IDs, and bare
references. All `--from` / `-f` operations route through this.

## Resolve-Before-Persist vs Row-First

- **Streaming-serve** (via `SpawnApplicationService.prepare_spawn()`): resolution
  first, row created only on success. This is the clean path.
- **Spawn subprocess** (`execute.py`): row created before calling `build_launch_context()`.
  This is a known gap; do not copy it into new paths.

## Anti-Patterns

**Don't build argv or compose env in ops/.** That's `launch/` territory.
If an ops function knows which CLI flags to pass to Claude, something is wrong.

**Don't add harness-specific logic to ops/.** Policy is harness-agnostic.

**Don't set `LaunchArgvIntent.REQUIRED` on execution paths.** Only
`ops/spawn/prepare.py` uses `REQUIRED` for dry-run display. All execution paths
use `SPEC_ONLY`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — SpawnApplicationService layer diagram,
   execution path ownership, SEAM-1/2/3 contracts, resolve-before-persist detail.

## Related

- `spawn/AGENTS.md` — spawn policy subpackage depth
- `../launch/AGENTS.md` — mechanism layer ops/spawn drives
- `../state/AGENTS.md` — state stores ops reads from
- KB `architecture/launch-system.md` — launch adapter architecture; ops/spawn is adapter #2 of three
