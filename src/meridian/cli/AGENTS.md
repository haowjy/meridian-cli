# cli/

CLI entry point, command groups, and startup pipeline. Descriptor-driven
routing: a `CommandCatalog` classifies every invocation before any heavy
module imports.

## Entry Points

- `entrypoint.py` — `main()`: handles trivial fast paths (help/version),
  delegates the rest to `main.py`
- `main.py` — full command dispatch via `_register_commands_for_invocation()`
- `app_tree.py` — top-level Cyclopts app instances (`spawn_app`, `session_app`, …)
  without importing command implementations

## Startup Submodule (`startup/`)

- `catalog.py` — `CommandDescriptor`, `CommandCatalog`, `COMMAND_CATALOG`
- `classify.py` — `classify_invocation()`: longest-prefix argv → descriptor
- `policy.py` — `StartupClass`, `StateRequirement`, `TelemetryMode`, `RootSource`
- `lazy_dispatch.py` — `make_lazy_command(lazy_target)`: deferred import adapter
- `cyclopts_app.py` — Cyclopts app construction with help profiles
- `help.py` — static help rendering for trivial fast path

## Command Modules

- `spawn.py` — `spawn` group commands
- `session_cmd.py` — `session` group commands
- `chat_cmd.py` — `chat` group commands
- `work_cmd.py` — `work` group commands
- `primary_launch.py` — bare `meridian` launch (primary harness mode)
- `ext_cmd.py` — extension CLI surface

Supporting modules: `bootstrap_cmd.py`, `config_cmd.py`, `doctor_cmd.py`,
`kg_cmd.py`, `mermaid_cmd.py`, `models_cmd.py`, `qi_cmd.py`,
`report_cmd.py`, `telemetry_cmd.py`, `workspace_cmd.py`, `misc_commands.py`

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Invocation class table and bootstrap mapping
- Lazy import strategy and measured startup improvements
- `app_tree.py` circular-import break pattern
- Descriptor-driven redirect (`models list` → `mars models list`)
- Help profile selection (human vs agent mode)
- `to_cli_output()` dispatch instead of `isinstance` branches

## Related

- `../lib/bootstrap/` — `prepare_for_*` functions called by bootstrap stage
- `../lib/core/resolved_context.py` — `ResolvedContext` for primary launch
- KB: [architecture/startup-pipeline.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/startup-pipeline.md)
