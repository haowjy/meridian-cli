# cli/ — Startup Pipeline and Import Strategy

## Descriptor-Driven Startup

Every CLI invocation is classified against `COMMAND_CATALOG` before any
heavy module loads. `CommandDescriptor` carries all routing information:

- `startup_class` — determines which bootstrap call to make
- `state_requirement` — `none | project-read | runtime-read | project-write | runtime-write`
- `telemetry_mode` — `none | stderr | segment`
- `lazy_target` — import path string for the Cyclopts handler
- `root_source` — `"cwd"` (default) or `"argv"` (post-parse root resolution)
- `redirect` — optional redirect policy (e.g. `models list` → `mars models list`)

The catalog is the single source of truth for routing. The CLI startup path
never queries the extension registry for routing answers.

## Invocation Class Table

| Class | Examples | Bootstrap | Telemetry |
|---|---|---|---|
| `TRIVIAL` | `--help`, `--version` | none | none |
| `READ_PROJECT` | `config show`, `work current` | project-read | none |
| `READ_RUNTIME` | `spawn list`, `session log` | runtime-read | none |
| `WRITE_PROJECT` | `config init` | project-write | optional |
| `WRITE_RUNTIME` | `spawn create`, `work start` | runtime-write | segment |
| `PRIMARY_LAUNCH` | bare `meridian` | runtime-write | segment |
| `SERVICE_ROOTLESS` | `serve` | none | stderr |
| `SERVICE_RUNTIME` | `chat` | runtime-write | segment |
| `CLIENT_READ` | `chat ls`, `chat show` | runtime-read | none |

Read-only classes install no telemetry, spawn no writer thread, create no
UUID, and make no filesystem mutations.

## Lazy Import Strategy

Four independent mechanisms reduce startup latency:

1. **Selective command registration** — `_register_commands_for_invocation()`
   imports only the module group matching the first positional token.
   Full registration fires only for `--help`.

2. **`lazy_dispatch.py`** — `make_lazy_command("module.path:function")` returns
   a callable that imports only when invoked. Cyclopts holds registrations
   without triggering imports.

3. **`app_tree.py` pattern** — defines top-level Cyclopts app objects
   (`spawn_app`, `session_app`, …) without importing any command implementations.
   Breaks the circular import where importing app objects pulled in all commands.

4. **Heavy module deferral** — `primary_launch` and `mars_passthrough` imported
   inside handler functions, not at module scope.

**Measured impact:** `main.py` module-import time 460ms → 156ms; root
`--help` latency 140ms → 54ms.

## Help Profile Selection

Human vs agent mode selects a `HelpProfile` at app-build time from the
command catalog — no runtime mutation of shared `App` objects. Do not call
`apply_agent_help_supplements()` (archived pattern).

## `to_cli_output()` Dispatch

Command handlers emit results through `to_cli_output()` dispatch rather
than `isinstance` branches in `main.py`. This keeps the root CLI module
free of spawn-op and output-model imports. Each result type implements
the small protocol to produce its wire-shaped output.

## Related KB

→ [KB: architecture/startup-pipeline.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/startup-pipeline.md)

## Lateral Links

→ [../../lib/bootstrap/.context/CONTEXT.md](../../lib/bootstrap/.context/CONTEXT.md) — bootstrap functions invoked after classification
