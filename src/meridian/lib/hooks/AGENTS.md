# lib/hooks — Lifecycle Event Callbacks

Hooks fire at spawn and work lifecycle events for side effects: syncing, notifying, triggering pipelines. They do not modify agent behavior — they observe it.

## Mental Model

Users configure hooks in TOML (project or user config). At each lifecycle event (`spawn.created`, `spawn.finalized`, `work.done`, etc.), `HookDispatcher.fire()` loads the hook list for that event, filters by conditions, checks interval throttling, and executes each hook in order. Hooks are either built-in (in-process) or external (subprocess).

The dispatcher is **fail-open** for `observe` and `post` event classes. A failing hook logs a warning and continues — it never stops subsequent hooks or the lifecycle event itself. Only `gate` class hooks can block (none currently registered).

Config layers in ascending precedence:
```
builtin defaults < context config < user (~/.meridian/config.toml) < project (meridian.toml) < local (meridian.local.toml)
```
Override key is `(name, event)` — a higher-priority row with the same key replaces the lower-priority one entirely.

## Key Rules

- **Fire `HookDispatcher.fire(context)` — don't instantiate the registry or runner directly.** `HookDispatcher.from_application_context(ctx)` is the preferred constructor.
- **Built-in hooks must import only from `meridian.plugin_api`.** Zero `meridian.lib.*` imports. This keeps built-ins usable as reference implementations for external plugins. If a built-in needs something not in the API, extend the plugin API.
- **`HookContext.to_env()` and `HookContext.to_json()` are the only correct serialization paths.** External subprocess hooks receive both env vars and JSON stdin. `None` values are omitted — no empty-string env vars.
- **Hook execution order within an event:** source rank (builtin first) → negative priority → declaration order. This is stable and deterministic.
- **`when.status` and `when.agent` conditions** are checked before interval throttling.

## Entry Points

- `dispatch.py` — `HookDispatcher.fire(context)`, `HookDispatcher.from_application_context(ctx)`
- `registry.py` — `HookRegistry` (loads config, serves event-scoped hook lists)
- `config.py` — `load_hooks_config(project_root, ...)` with source layering
- `types.py` — `Hook`, `HookContext`, `HookResult`, `HookEventName`
- `builtin/` — built-in hook implementations (`git_autosync.py` is the primary one)

## Anti-Patterns

- Don't import from submodules directly — `from meridian.lib.hooks import HookDispatcher` works via lazy `__getattr__` in `__init__.py`. This avoids circular import issues at startup.
- Don't set `failure_policy = "fail"` on observe-class hooks without deliberate intent — fail-open is the correct default for observation.
- Don't use `meridian.local.toml` in version control — it's for machine-specific overrides.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — config layering, event classes, failure behavior, plugin API boundary, context transport schema
→ [KB: concepts/hooks-and-plugins.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/concepts/hooks-and-plugins.md)
