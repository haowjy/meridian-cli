# lib/ops/

Operation handlers — the policy layer between CLI/MCP surfaces and mechanism
layers (launch, state, harness). Each file implements one operational concern.
CLI commands and MCP tools call into ops; ops calls into `launch/`, `state/`,
and `harness/`.

## Spawn Operations (`spawn/`)

The primary subpackage. Start here for anything related to creating, tracking,
or managing spawns.

- `spawn/api.py` — public surface: `spawn_create`, `spawn_list`, `spawn_show`,
  `spawn_wait`, `spawn_cancel`, `spawn_fork`, `spawn_continue`, `spawn_stats`
  (sync and async variants for each)
- `spawn/execute.py` — execution helpers: `execute_spawn_blocking()`,
  `execute_spawn_background()`, `launch_prepared_spawn()`
- `spawn/prepare.py` — `build_create_payload()`, `validate_create_input()`;
  `SPAWN_PREPARE` surface + `LaunchArgvIntent.REQUIRED` (dry-run display only)
- `spawn/models.py` — input/output Pydantic models; `SpawnCreateInput`,
  `SpawnListEntry`, `SpawnDetailOutput`, etc.
- `spawn/context_ref.py` — `--from` reference resolution; renders prior output
  into spawn context
- `spawn/failure_policy.py` — `finalize_launch_failure()`: pre-run failure finalization

## Session Operations

- `session_read.py`, `session_log.py`, `session_render.py` — read and render session transcripts
- `session_search.py` — search session content
- `session_target.py` — resolve session targets (by spawn ID, chat ID, or reference)
- `session_export.py` — export sessions
- `session_repair.py` — repair malformed session records

## Work and Workspace

- `work_lifecycle.py` — work item CRUD (`work_create`, `work_list`, `work_show`, etc.)
- `work_attachment.py` — `ensure_explicit_work_item()`: attach spawns to work items
- `work_dashboard.py` — dashboard rendering for active work items
- `workspace.py` — workspace snapshot management
- `worktree_lifecycle.py`, `worktree_ops.py` — git worktree operations
- `worktree_format.py` — worktree output formatting

## Configuration and Catalog

- `config.py`, `config_surface.py` — config get/set/show/init/reset operations
- `catalog.py` — `models_list()`: model alias catalog queries
- `mars.py` — `meridian mars ...` delegation to mars binary

## Other Operations

- `commands.py` — explicit operation manifest shared by CLI and MCP surfaces
- `runtime.py` — `OperationRuntime`, `build_runtime()`, runtime root resolution helpers
- `reference.py`, `reference_recovery.py` — session reference resolution
- `report.py` — spawn report operations
- `qi.py` — `meridian qi` inline knowledge navigation
- `hooks.py` — lifecycle hook execution
- `diag.py` — diagnostic operations
- `migration.py` — state migration helpers
- `pruning.py` — spawn/session pruning operations
- `context.py` — operation context resolution

## Depth Reference

- `.context/CONTEXT.md` — ops/launch boundary, SpawnApplicationService role,
  execution path ownership, key invariants

## Related

- `spawn/` — spawn subpackage depth: `spawn/.context/` (if present)
- `../launch/` — mechanism layer ops/spawn drives
- `../state/` — state stores ops reads from
- KB `architecture/launch-system.md` — four-adapter diagram; ops/spawn is adapter #2
- KB `concepts/spawn-lifecycle.md` — spawn status machine ops surfaces expose
