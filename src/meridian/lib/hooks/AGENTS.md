# lib/hooks — Hook Dispatch

User-defined callbacks that fire at spawn and work lifecycle events. Used for
side effects (sync, notify, trigger pipelines) — not for modifying agent behavior.

## Key Components

- `HookDispatcher` — coordinates hook execution for one event (`dispatch.py`)
- `HookRegistry` — loads config + serves event-scoped hook lists (`registry.py`)
- `load_hooks_config` — TOML config loading with source layering (`config.py`)
- `Hook`, `HookContext`, `HookResult`, `HookEventName` — core types (`types.py`)
- `builtin/` — built-in hook implementations; `git_autosync.py` is the primary one

## Entry Points

- `HookDispatcher.fire(context: HookContext) → list[HookResult]` — execute all hooks for an event
- `HookDispatcher.from_application_context(ctx)` — preferred constructor in application code
- `load_hooks_config(project_root, ...)` — load merged hook config from all sources
- `HookContext.to_env()` — serialize context as `MERIDIAN_*` env vars for subprocess hooks
- `HookContext.to_json()` — serialize context as JSON for stdin transport

## Events

`spawn.created`, `spawn.running`/`spawn.start`, `spawn.finalized`, `work.start`/`work.started`, `work.done`

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — config layering, event classes, plugin API boundary
→ [KB: concepts/hooks-and-plugins.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/concepts/hooks-and-plugins.md)
