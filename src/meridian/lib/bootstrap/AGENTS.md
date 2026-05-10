# lib/bootstrap/

Bootstrap facade: prepares project and runtime state before command
handlers run. Enforces the `resolve_*` (pure) / `ensure_*` (may mutate)
split that keeps read-only command paths side-effect-free.

## Entry Points

- `services.py` — `prepare_for_project_read()`, `prepare_for_runtime_read()`,
  `prepare_for_project_write()`, `prepare_for_runtime_write()` — the four
  bootstrap entrypoints. Pick based on what the command needs.
- `services.py` — `build_spawn_entrypoint()`, `build_chat_entrypoint()`,
  `build_extension_entrypoint()`, `build_spawn_application_service()`
- `project_state.py` — `ProjectLayoutSnapshot`, `resolve_layout()`,
  `ensure_project_dirs()`, `ensure_project_gitignore()`
- `runtime_state.py` — `ensure_runtime_root()`, `ensure_runtime_dirs()`
- `config.py` — `load_config()`, `load_context_snapshot()`

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Which bootstrap function to call for which command class
- `resolve_*` vs `ensure_*` contract
- `ProjectReadContext` → `RuntimeWriteContext` hierarchy
- `ApplicationContext` carrier and entrypoint shapes

## Related

- `../cli/startup/` — invocation classifier that determines which bootstrap call
- `../lib/ops/runtime.py` — `RuntimeAuthoritySnapshot` (authority model)
- KB: [architecture/startup-pipeline.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/startup-pipeline.md)
