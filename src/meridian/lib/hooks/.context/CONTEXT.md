# lib/hooks — Contracts and Architecture

## Architecture

```
HookDispatcher.fire(context: HookContext)
  └── HookRegistry.get_hooks_for_event(event)
        ← load_hooks_config(project_root)  [config.py]
              layered: builtin < user < project < local
              override key: (name, event)
  └── for each Hook (ordered by source rank, priority, declaration order):
        _check_conditions(hook, context)     when.status / when.agent filters
        IntervalTracker.should_run(name, interval)   throttle check
        _run_builtin(hook, context)          OR
        ExternalHookRunner.run(hook, context, timeout_secs=...)
  └── returns list[HookResult]
```

## Config Layering

Hook sources in ascending precedence:

```
builtin defaults
  ↑ context config
  ↑ user config  (~/.meridian/config.toml)
  ↑ project config  (meridian.toml)
  ↑ local config  (meridian.local.toml)   ← highest
```

Override key is `(name, event)`. A higher-priority source row with the same key replaces the lower-priority row entirely. Use `meridian.local.toml` for machine-specific overrides without committing them.

## Event Classes and Failure Behavior

`EVENT_CLASS` maps each event to one of three classes:

| Class | Events | Default failure policy | Fail-open? |
|-------|--------|----------------------|-----------|
| `observe` | `spawn.created`, `spawn.running`, `spawn.start`, `work.start`, `work.started` | `warn` | Yes |
| `post` | `spawn.finalized`, `work.done` | `warn` | Yes |
| `gate` | (none currently registered) | `fail` | No |

`gate` class hooks stop the dispatch loop on failure. `observe` and `post` hooks are always fail-open: a hook failure logs a warning but does not stop subsequent hooks or the lifecycle event. Individual hooks can override with `failure_policy = "fail"`.

## Plugin API Boundary

**Built-in hooks must import only from `meridian.plugin_api`** — zero imports from `meridian.lib.*`. This is an enforced discipline: the plugin API is a stable boundary that external plugins can also use. If a built-in needs something not in the API, extend the API rather than breaking the boundary.

The dispatcher bridges internal `HookContext` to plugin `PluginHookContext` via `_to_plugin_context()` and `_to_plugin_hook()` in `dispatch.py`. Built-ins see only the plugin surface.

## Hook Execution Order

Within an event, hooks are sorted by:
1. Source rank (`builtin` → `context` → `user` → `project` → `local`, lower = earlier)
2. Negative priority (higher `priority` value = earlier execution)
3. Declaration order (stable sort)

## `HookWhen` Conditions

`when.status` filters by spawn terminal status (e.g., only fire on `success`). `when.agent` filters by agent profile name. Both are optional; if absent, the hook fires unconditionally for that event. Conditions are checked before interval throttling.

## Context Transport

`HookContext.to_env()` emits `MERIDIAN_HOOK_*`, `MERIDIAN_SPAWN_*`, `MERIDIAN_ACTIVE_WORK_*` env vars. `None` values are omitted (no empty string env vars). External hooks receive both env vars and JSON on stdin — the JSON schema includes a `schema_version` field (`HOOK_CONTEXT_SCHEMA_VERSION = 1`) for forward compatibility.

## `__init__.py` Lazy Loading

`lib/hooks/__init__.py` uses `__getattr__` with an `_EXPORTS` dispatch table to load submodules lazily. This avoids import-time circular dependencies. All types are available via `from meridian.lib.hooks import HookDispatcher` etc. — importers don't need to know the internal module structure.

## Related KB

→ [KB: concepts/hooks-and-plugins.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/concepts/hooks-and-plugins.md)
