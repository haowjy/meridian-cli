# lib/bootstrap/ — Bootstrap Facade

Prepares project and runtime state before command handlers run, through four entry
points: `prepare_for_project_read()`, `prepare_for_runtime_read()`,
`prepare_for_project_write()`, `prepare_for_runtime_write()`. Which startup class calls
which, and the context-object hierarchy: `.context/CONTEXT.md`.

## Rules

- **`resolve_*` is pure; `ensure_*` may mint identity and create state.** Read paths call
  only `resolve_*`; calling `ensure_*` from a read path silently creates identity,
  directories, or config. Pick the lowest tier the command needs.
- **Runtime-write preparation recovers incomplete spawn publication** by removing
  abandoned `spawns/.staging/*` under the spawn allocation/publication lock; read
  preparation never runs this GC.
- **One authority per context.** Each `prepare_for_*` resolves a `RuntimeAuthoritySnapshot`
  once and passes it to `load_config(..., authority=authority)`; config resolved without
  it re-derives paths and can diverge from a frozen or custom snapshot.
- **Two-tier identity on first write**: a marked project mints its committed ID; a
  marker-less path materializes path-derived state under the user home. Runtime writes
  never create `meridian.toml`.
- **`RootSource.ARGV` commands** (`meridian init [path]`) call
  `prepare_for_project_write(arg_derived_root)` themselves after parsing; all others
  receive a pre-prepared context.
- **Build services through the factories**: `build_spawn_application_service(runtime_write_ctx)`,
  `build_extension_entrypoint()`; don't instantiate
  `SpawnApplicationService` or the entrypoint carriers by hand.

KB: `architecture/startup-pipeline.md`.
