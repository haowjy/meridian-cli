# Extension Command Authoring

Extension commands are the canonical way to add operations that must appear on multiple surfaces — CLI, HTTP API, and MCP — without duplicating routing logic. Define once, project everywhere.

## Architecture

```
ExtensionCommandSpec (defined in code)
        │
        ▼
ExtensionCommandRegistry (build_first_party_registry)
        │
        ├──▶ CLI:  meridian ext run / ext list / ext commands
        ├──▶ HTTP: POST /api/extensions/{ext_id}/commands/{cmd_id}/invoke
        └──▶ MCP:  extension_invoke tool
```

The `ExtensionCommandDispatcher` handles validation and observability for all three paths. Handlers never know which surface called them — they receive a uniform `(args, context, services)` signature.

## Two Registration Paths

### op-style (preferred)

Use `ExtensionCommandSpec.from_op()` to wrap an existing async handler that takes a typed Pydantic model and returns a result model. This is the right path when:

- the handler is already written as `async def handler(input: MyInput) -> MyOutput`
- you want a sync variant for CLI use without a running event loop

```python
from pydantic import BaseModel, Field
from meridian.lib.extensions.types import ExtensionCommandSpec, ExtensionSurface


class SpawnArchiveInput(BaseModel):
    spawn_id: str = Field(description="Spawn to archive")


class SpawnArchiveOutput(BaseModel):
    spawn_id: str
    archived: bool


async def _archive_handler(input: SpawnArchiveInput) -> SpawnArchiveOutput:
    # business logic
    return SpawnArchiveOutput(spawn_id=input.spawn_id, archived=True)


def _archive_sync(input: SpawnArchiveInput) -> SpawnArchiveOutput:
    # sync version for CLI
    return SpawnArchiveOutput(spawn_id=input.spawn_id, archived=True)


SPEC = ExtensionCommandSpec.from_op(
    handler=_archive_handler,
    sync_handler=_archive_sync,       # omit if sync use not needed
    input_type=SpawnArchiveInput,
    output_type=SpawnArchiveOutput,
    extension_id="meridian.sessions",
    command_id="archiveSpawn",
    summary="Archive a completed spawn to hide it from default listings",
    cli_group="spawn",                # optional: routes as `meridian spawn archive`
    cli_name="archive",
    requires_app_server=False,        # True = app server must be running
    first_party=True,
)
```

### direct ExtensionCommandSpec

Use the constructor directly when the handler needs the full 3-arg signature — for example, to access `context` (request/work/spawn IDs) or `services` (runtime root, meridian dir):

```python
from typing import Any
from meridian.lib.extensions.context import ExtensionCommandServices, ExtensionInvocationContext
from meridian.lib.extensions.types import (
    ExtensionCommandSpec,
    ExtensionErrorResult,
    ExtensionJSONResult,
    ExtensionResult,
    ExtensionSurface,
)


async def my_handler(
    args: dict[str, Any],
    context: ExtensionInvocationContext,
    services: ExtensionCommandServices,
) -> ExtensionResult:
    if services.runtime_root is None:
        return ExtensionErrorResult(code="service_unavailable", message="runtime_root missing")

    # use args, context.work_id, context.spawn_id, context.request_id, services.runtime_root
    return ExtensionJSONResult(payload={"ok": True})


MY_SPEC = ExtensionCommandSpec(
    extension_id="meridian.myext",
    command_id="doSomething",
    summary="One-line description for CLI/MCP help",
    args_schema=MyArgsModel,
    result_schema=MyResultModel,
    handler=my_handler,
    surfaces=frozenset({ExtensionSurface.CLI, ExtensionSurface.HTTP, ExtensionSurface.MCP}),
    first_party=True,
    requires_app_server=True,
)
```

### Handler return types

| Return value | Treated as |
| ------------ | ---------- |
| `ExtensionJSONResult(payload={...})` | Success — payload forwarded to caller |
| `ExtensionErrorResult(code=..., message=...)` | Structured error — surfaces map to appropriate HTTP status |
| `dict` | Automatically wrapped in `ExtensionJSONResult` |
| Any other value | Wrapped as `{"result": value}` |

## Surfaces and Capabilities

### Surfaces

```python
from meridian.lib.extensions.types import ExtensionSurface

# All three (default)
surfaces=frozenset({ExtensionSurface.HTTP, ExtensionSurface.CLI, ExtensionSurface.MCP})

# MCP only — for agent-facing commands that shouldn't be shell-scriptable
surfaces=frozenset({ExtensionSurface.MCP})
```

Only `first_party=True` commands may appear on CLI or MCP. Third-party commands are HTTP-only.

### Capabilities

Commands that need elevated access declare `required_capabilities`. The dispatcher enforces these before calling the handler.

```python
required_capabilities=frozenset({"subprocess"})
```

Available capabilities:

| Capability | Granted by |
| ---------- | ---------- |
| `subprocess` | HTTP surface (app server context) |
| `kernel` | HTTP surface (app server context) |
| `hitl` | HTTP surface (app server context) |

CLI and MCP callers receive no capabilities. Commands that declare `required_capabilities` will fail with `capability_missing` on those surfaces.

## Registering the Command

All first-party commands are registered in `src/meridian/lib/extensions/first_party.py`:

```python
from meridian.lib.extensions.registry import ExtensionCommandRegistry


def register_first_party_commands(registry: ExtensionCommandRegistry) -> None:
    from meridian.lib.extensions.commands.myext import MY_SPEC
    registry.register(MY_SPEC)

    # ... existing registrations
```

**Validation on register.** The registry raises `ValueError` for:
- Duplicate fqids (`extension_id.command_id`)
- Duplicate CLI routes (`cli_group` + `cli_name` pair)
- Third-party commands with CLI or MCP surfaces

## CLI routing

Set `cli_group` and `cli_name` to graft the command onto an existing CLI group:

```python
cli_group="spawn",
cli_name="archive",
```

This makes the command callable as `meridian spawn archive <args>` in addition to `meridian ext run meridian.sessions.archiveSpawn`. Both `cli_group` and `cli_name` must be set together or both left as `None`.

Set `default_output_mode` on the matching `CommandDescriptor` in `cli/startup/catalog.py` to choose JSON vs text when the CLI runs in agent mode without an explicit `--format`.

## requires_app_server

| Value | Meaning |
| ----- | ------- |
| `False` | Runs in-process on all surfaces — no app server needed |
| `True` | CLI and MCP locate the running app server and proxy via HTTP; HTTP handler is invoked directly on the server |

Commands that access runtime state (spawns, sessions, work items) set `requires_app_server=True`. Pure computation or read-only catalog commands can use `False`.

## FQID convention

`extension_id` uses reverse-domain dot notation: `meridian.sessions`, `meridian.workbench`. `command_id` uses camelCase: `archiveSpawn`, `getSpawnStats`. The fully qualified ID is `extension_id.command_id`, e.g. `meridian.sessions.archiveSpawn`.

Keep `summary` under 80 characters — it appears in `meridian ext commands`, MCP tool descriptions, and HTTP discovery schemas.
