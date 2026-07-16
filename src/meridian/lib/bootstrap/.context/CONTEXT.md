# lib/bootstrap — Contracts and Architecture

## The `resolve_*` / `ensure_*` Split

This is the central invariant. Violating it causes read-only command paths
to silently create filesystem state (UUIDs, directories, `.gitignore` entries).

| Prefix | Contract |
|---|---|
| `resolve_*` | Pure — no filesystem mutations. Returns `None` if state doesn't exist. |
| `ensure_*` | May create directories, UUIDs, `.gitignore` entries. Only call on write paths. |

`prepare_for_project_read()` and `prepare_for_runtime_read()` call only
`resolve_*` helpers. `prepare_for_project_write()` and
`prepare_for_runtime_write()` call `ensure_*` helpers.

`ensure_runtime_dirs()` also performs crash recovery for spawn publication: under
`spawns_flock`, it removes only abandoned entries beneath `spawns/.staging/`.
Runtime-read preparation does not mutate or collect this state.

## Which Bootstrap Function to Call

Pick based on the `StartupClass` of the command (from `cli/startup/policy.py`):

| Startup class | Bootstrap function |
|---|---|
| `TRIVIAL` | None |
| `READ_PROJECT` | `prepare_for_project_read()` |
| `READ_RUNTIME` | `prepare_for_runtime_read()` |
| `WRITE_PROJECT` | `prepare_for_project_write()` |
| `WRITE_RUNTIME`, `SERVICE_RUNTIME` | `prepare_for_runtime_write()` |
| `PRIMARY_LAUNCH` | `prepare_for_runtime_write()`; `bootstrap_plan.auto_init_cwd` auto-inits config first |
| `SERVICE_ROOTLESS` | None (or minimal manual setup) |

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

## `ApplicationContext` and Entrypoints

`build_spawn_entrypoint()`, `build_extension_entrypoint()`
convert prepared contexts into typed entrypoint carriers. These carriers
(`SpawnEntryPoint`, `ExtensionEntryPoint`) carry
`ApplicationContext + ApplicationServices` downstream without exposing
bootstrap internals to command handlers.

`build_spawn_application_service()` takes a `RuntimeWriteContext` and builds
the full `SpawnApplicationService`. This is the correct path for commands
that need to create or manage spawns. Do not instantiate `SpawnApplicationService`
directly — use this factory.

## Post-Parse Bootstrap

Commands with `root_source = RootSource.ARGV` (e.g. `meridian init [path]`)
defer bootstrap until after argument parsing. They call
`prepare_for_project_write(arg_derived_root)` themselves rather than
receiving a pre-prepared context. This is a narrow exception — most
commands use the cwd-derived root.

## Related KB

→ [KB: architecture/startup-pipeline.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/startup-pipeline.md)
