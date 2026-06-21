# lib/bootstrap/ — Bootstrap Facade

Prepares project and runtime state before command handlers run. Enforces a strict `resolve_*` / `ensure_*` split: read-only command paths never mutate the filesystem.

## Mental Model

Four entry points, one per invocation class tier. Pick based on what the command needs — not based on what's convenient:

| Startup class | Call |
|---|---|
| `READ_PROJECT` | `prepare_for_project_read()` |
| `READ_RUNTIME` | `prepare_for_runtime_read()` |
| `WRITE_PROJECT` | `prepare_for_project_write()` |
| `WRITE_RUNTIME`, `PRIMARY_LAUNCH`, `SERVICE_RUNTIME` | `prepare_for_runtime_write()` |

Each function returns a typed context object (`ProjectReadContext`, `RuntimeReadContext`, `ProjectWriteContext`, `RuntimeWriteContext`). These carry the prepared state downstream to command handlers without exposing bootstrap internals.

## The `resolve_*` / `ensure_*` Split

This is the central invariant. Violating it causes read-only command paths to silently create filesystem state (UUIDs, directories, `.gitignore` entries).

- **`resolve_*`**: pure — no filesystem mutations. Returns `None` if state doesn't exist.
- **`ensure_*`**: may create directories, UUIDs, `.gitignore` entries. Call only on write paths.

`prepare_for_project_read()` and `prepare_for_runtime_read()` call only `resolve_*`. The `prepare_for_*_write()` functions call `ensure_*`.

## Key Rules

- **Don't instantiate `SpawnApplicationService` directly.** Use `build_spawn_application_service(runtime_write_ctx)`. It's the correct factory.
- **Post-parse bootstrap exception**: commands with `root_source = RootSource.ARGV` (e.g. `meridian init [path]`) call `prepare_for_project_write(arg_derived_root)` themselves after argument parsing. All other commands receive a pre-prepared context.
- **Entrypoint carriers** (`SpawnEntryPoint`, `ExtensionEntryPoint`) carry `ApplicationContext + ApplicationServices` downstream. Use `build_spawn_entrypoint()`, `build_extension_entrypoint()` — don't assemble them manually.

## Context Hierarchy

```
ProjectReadContext
  ├── authority: RuntimeAuthoritySnapshot
  ├── project_root: Path
  ├── layout: ProjectLayoutSnapshot
  └── config: MeridianConfig | None

RuntimeReadContext(ProjectReadContext)
  └── runtime_root: Path | None   ← None if project has no UUID yet

ProjectWriteContext(ProjectReadContext)
  └── project_dirs_ensured: bool = True

RuntimeWriteContext(RuntimeReadContext)
  ├── project_dirs_ensured: bool = True
  └── runtime_dirs_ensured: bool = True
```

## Entry Points

- `services.py` — the four `prepare_for_*()` functions and entrypoint builders
- `project_state.py` — `ProjectLayoutSnapshot`, `resolve_layout()`, `ensure_project_dirs()`
- `runtime_state.py` — `ensure_runtime_root()`, `ensure_runtime_dirs()`
- `config.py` — `load_config()`, `load_context_snapshot()`

## Anti-Patterns

- Don't call `ensure_*` helpers from read-only bootstrap paths — it silently creates state that confuses idempotency checks.
- Don't pick a higher bootstrap tier than needed — `prepare_for_runtime_write()` in a read-only command wastes work and risks side effects.

## Related

- `../cli/startup/` — invocation classifier that determines which bootstrap call to make
- `../lib/ops/runtime.py` — `RuntimeAuthoritySnapshot`

→ [.context/CONTEXT.md](.context/CONTEXT.md) — resolve/ensure split detail, context hierarchy, entrypoint shapes, post-parse bootstrap pattern
→ [KB: architecture/startup-pipeline.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/startup-pipeline.md)
