# cli/ — CLI Entry Point and Startup Pipeline

Command-line surface. Descriptor-driven: every invocation is classified before any heavy module imports. Fast paths stay fast; heavy operations load only when needed.

## Mental Model

```
argv
  └── normalize_optional_value_flags()  ← pre-Cyclopts: rewrites bare --fork/--from
        └── classify_invocation()       ← longest-prefix match against COMMAND_CATALOG
              └── CommandDescriptor     ← routing information for this invocation
                    ├── startup_class   → which bootstrap function to call
                    ├── state_requirement
                    ├── telemetry_mode
                    ├── lazy_target     → import path string (deferred import)
                    └── redirect        → optional redirect (e.g. models list → mars models list)
```

`COMMAND_CATALOG` in `startup/catalog.py` is the single source of truth for routing. The startup path never queries the extension registry or any command module for routing decisions. Classification happens before anything expensive loads.

## Key Rules

- **Lazy imports everywhere.** `_register_commands_for_invocation()` imports only the module group matching the first positional token. Full registration fires only for `--help`. `make_lazy_command("module.path:function")` in `lazy_dispatch.py` registers a Cyclopts handler without importing it.
- **`app_tree.py` breaks the circular import.** It defines top-level Cyclopts app objects (`spawn_app`, `session_app`, etc.) without importing any command implementations. If you import command implementations from `app_tree.py`, the lazy strategy collapses.
- **`to_cli_output()` dispatch, not `isinstance` in `main.py`.** Command handlers return typed result objects. Each implements `to_cli_output()`. Adding result types does not require editing `main.py`.
- **Read-only invocation classes install no telemetry, create no filesystem state.** `READ_PROJECT` and `READ_RUNTIME` classes never write UUIDs, create directories, or spawn writer threads.
- **`root_source = RootSource.ARGV`** commands (e.g. `meridian init [path]`) defer bootstrap until after argument parsing. They call `prepare_for_project_write(arg_derived_root)` themselves — they don't receive a pre-prepared context.
- **`validate_fork_mode()` is the single place for fork/from/continue conflict checks.** `spawn.py` and `primary_launch.py` both call it and consume `ForkModeResolution`. Do not re-implement conflict logic or ref resolution in handlers.
- **`normalize_optional_value_flags()` runs before everything.** It rewrites bare `--fork`/`--fork-fresh`/`--from` to `--fork __SELF__` so Cyclopts always receives a value. Never call Cyclopts on raw argv containing these flags.

## Invocation Classes

| Class | Examples | Bootstrap |
|---|---|---|
| `TRIVIAL` | `--help`, `--version` | none |
| `READ_PROJECT` | `config show`, `work current` | `prepare_for_project_read()` |
| `READ_RUNTIME` | `spawn list`, `session log` | `prepare_for_runtime_read()` |
| `WRITE_PROJECT` | `config init` | `prepare_for_project_write()` |
| `WRITE_RUNTIME` | `spawn create`, `work start` | `prepare_for_runtime_write()` |
| `PRIMARY_LAUNCH` | bare `meridian` | `prepare_for_runtime_write()` |
| `SERVICE_ROOTLESS` | `serve` | none |
| `SERVICE_RUNTIME` | `streaming serve` | `prepare_for_runtime_write()` |

## Entry Points

- `entrypoint.py` — `main()`: trivial fast paths (help/version), delegates to `main.py`
- `main.py` — full dispatch via `_register_commands_for_invocation()`
- `app_tree.py` — Cyclopts app objects without command implementations
- `argv_normalization.py` — pre-Cyclopts rewriting of `--fork`/`--fork-fresh`/`--from`; `validate_fork_mode()`; `ForkModeResolution`
- `startup/catalog.py` — `COMMAND_CATALOG`, `CommandDescriptor`
- `startup/classify.py` — `classify_invocation()`
- `startup/lazy_dispatch.py` — `make_lazy_command()`

## Anti-Patterns

- Don't add heavy imports at module scope in command modules — they load on first invocation, not at startup.
- Don't add routing logic in `main.py` outside of the catalog — the descriptor carries all routing decisions.
- Don't mutate shared `App` objects for help profile selection — `HelpProfile` is selected at build time per invocation.
- Don't re-implement fork/continue/from conflict checks in handlers — call `validate_fork_mode()` and use `ForkModeResolution`.
- Don't push the identity lock (`--fork` + model/agent/skills conflict) into the ops layer — it's CLI ergonomic policy, not an ops invariant.

## Related

- `../lib/bootstrap/` — bootstrap functions called after classification
- `../lib/core/resolved_context.py` — `ResolvedContext` for primary launch

→ [.context/CONTEXT.md](.context/CONTEXT.md) — invocation class table, lazy import strategy, session initiation argument handling, app_tree circular-import pattern, help profile selection
→ [KB: architecture/startup-pipeline.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/startup-pipeline.md)
