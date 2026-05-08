# ops/ — Operation Commands and Shared Surface

`src/meridian/lib/ops/` is the API layer between the user-facing surfaces (CLI, MCP) and the lib subsystems. It owns the explicit operation manifest, all operation handler implementations, and the shared business logic for each command.

## Manifest Architecture

`ops/commands.py` defines every operation as an `OperationSpec` instance:

```python
class OperationSpec(Generic[InputT, OutputT]):
    name: str                  # dotted name: "spawn.create"
    description: str
    handler: async Callable    # async handler (used by MCP and async CLI paths)
    sync_handler: Callable     # sync handler (used by CLI registration)
    input_type: type[InputT]   # Pydantic model
    output_type: type[OutputT] # Pydantic model
    cli_group: str | None      # CLI command group ("spawn", "work", ...)
    cli_name: str | None       # CLI subcommand name ("create", "list", ...)
    mcp_name: str | None       # MCP tool name ("spawn_create", ...)
    surfaces: frozenset        # {"cli"}, {"mcp"}, or {"cli", "mcp"}
```

All ops live in the `_OPERATIONS` tuple. `get_operations_for_surface(surface)` filters by surface membership.

The manifest's validation enforces: CLI ops must have `cli_group` + `cli_name`; MCP ops must have `mcp_name`; every op exposes at least one surface.

**Why a manifest?** Without it, CLI and MCP surfaces drift: operations get added to one but not the other, descriptions diverge, input/output types duplicate. The manifest is the anti-drift constraint — one definition, two surfaces.

## CLI Registration

`cli/registration.py:register_manifest_cli_group()`:
- Iterates ops for surface "cli" matching the requested group
- For ops with an explicit handler in the `handlers` dict: use it
- For ops where `input_type` has no required non-implicit fields: auto-generate a handler that calls `sync_handler(input_type())`
- For ops with required fields and no explicit handler: raise ValueError

Auto-generation works because many operations (e.g., `models.refresh`, `doctor`) have no required user input — their input models default to `None` everywhere. Operations with required args (e.g., `spawn.show` needing a spawn ID) always have explicit handlers in the respective `cli/*.py` module.

`project_root` is an implicit field — it's always injected at runtime from `resolve_project_root()`, never provided by the user. The registration layer knows to skip it when checking whether a handler can be auto-generated.

**Why cyclopts?** The CLI uses cyclopts rather than argparse/click because cyclopts can derive CLI args from Python type annotations, which meshes cleanly with Pydantic input models. This keeps the explicit handler implementations thin — they translate cyclopts kwargs to the input model, call the sync handler, and emit the output.

## MCP Registration

`server/main.py`:
- Iterates ops for surface "mcp" at module import time (not lazily)
- For each op, builds a `_tool(**kwargs)` async function:
  1. `coerce_input_payload(op.input_type, kwargs)` — dict → Pydantic model, with type coercion
  2. `await op.handler(payload)` — runs the async operation
  3. Returns `result.to_wire()` if available, else `to_jsonable(result)`
- Registers with `mcp.tool(name=op.mcp_name, description=op.description)(_tool)`
- The tool's `__signature__` is synthesized from the input model so FastMCP generates correct JSON schema for the tool

The MCP server runs as `meridian serve` over stdio (JSON-RPC/MCP protocol). Clients connect to it as a tool provider.

## Surface Allocation

| Operation group | CLI | MCP | Notes |
|-----------------|-----|-----|-------|
| config.* | yes | no | Config mutation is a human/CLI concern |
| context | yes | yes | Context path query; used by spawned agents to discover work/kb roots |
| spawn.create | no | yes | Default action of bare `meridian spawn`, not a subcommand |
| spawn.continue | no | yes | MCP-only: agents continue spawns |
| spawn.cancel/list/show/stats/wait | yes | yes | Shared surface |
| spawn.files/children | yes | no | Utility; agents use report/show instead |
| session.log/search | yes | no | Conversation/progress log access is CLI/human concern |
| work.* | yes | no | Work lifecycle is CLI-managed |
| report.create/show/search | yes | yes | Agents write reports; MCP needs create |
| models.list/refresh | yes | yes | Both surfaces need model catalog |
| doctor | yes | yes | Diagnostics useful from MCP clients |

## ops/ Module Layout

```
ops/
  commands.py          Operation registry and OperationSpec definition
  catalog.py          models_list, models_refresh handlers
  config.py           config_get/set/reset/show/init + ensure_state_bootstrap_sync + mars init
  context.py          context query: resolved work/kb paths and sources (CLI + MCP)
  diag.py             doctor: session lock repair, orphan detection, missing .agents warnings
  report.py           report_create/show/search handlers
  reference.py        --from / --file reference resolution, @path expansion
  runtime.py          Shared runtime context helpers
  session_log.py      session log: compaction-aware paged log reader; falls back to spawn output
  session_search.py   session search: multi-segment text scan with navigation hints
  work_attachment.py  Session-to-work-item attachment tracking
  work_dashboard.py   work_list/show/sessions: grouped spawn dashboard
  work_lifecycle.py   work_start/done/delete/rename/reopen/switch/update/clear
  spawn/
    api.py            spawn_create/cancel/continue/list/show/stats/wait/files handlers
    prepare.py        Validation, model resolution, profile loading, prompt assembly
```

## Operation Flow for spawn_create

Spawn create is the most complex operation — it illustrates how ops/commands/launch connect:

1. `cli/spawn.py` explicit handler → constructs `SpawnCreateInput` from CLI flags, calls `spawn_create_sync`
2. `ops/spawn/api.py:spawn_create_sync` → calls `prepare_spawn()` then dispatches to background or blocking `launch_primary()`
3. `ops/spawn/prepare.py:prepare_spawn()`:
   - Validates input (model exists, no conflicting flags)
   - Resolves model alias via mars + pattern fallback
   - Loads agent profile and requested skills from `.agents/`
   - Renders `--file` references and `--from` prior context into prompt
   - Computes permission/approval policy
   - Returns `PreparedSpawn` ready for the launch layer
4. `lib/launch/` takes over from there (see `../launch/overview.md`)

## Config Bootstrap and Mars Init

`ops/config.py:ensure_runtime_state_bootstrap_sync()` runs on `meridian init` and lazily during first spawn:
- Creates the configured repo context directories (default `.meridian/kb/`, `.meridian/work/`, `.meridian/archive/work/`) and runtime directories (`spawns/`, `sessions/`, `artifacts/`, `cache/`) under the project runtime root
- Seeds `.meridian/.gitignore` (non-destructive: keeps tracked entries, removes deprecated ones)
- Calls `_ensure_mars_init()`:
  - If no `mars.toml`: runs `mars init [--link ...]` to scaffold an empty managed root
  - If `mars.toml` exists but `--link` dirs requested: runs `mars link <dir>` for each
- Scaffolds `.meridian/config.toml` with commented defaults if absent

This is the integration point where state initialization and mars bootstrap happen together. See `../mars/overview.md` for the full mars integration picture.
