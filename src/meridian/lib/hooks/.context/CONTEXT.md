# lib/hooks — Contracts and Architecture

## Architecture

```
HookDispatcher.fire(context: HookContext)
  └── HookRegistry.get_hooks_for_event(event)
        ← load_hooks_config(project_root)  [config.py]
              validates each row once as HookSpec  [schema.py]
              layered: user < project < local
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
default  (Meridian's own rows: a `notify` route per notification event)
  ↑ user config  (~/.meridian/config.toml)
  ↑ project config  (meridian.toml)
  ↑ local config  (meridian.local.toml)   ← highest
```

The `default` layer holds only the `notify` routes, so notifications deliver
with no config at all; it overrides like any layer. Each builtin hook object
(`builtin/`) owns its default events, interval, options model and the options
that name an unnamed row; they apply only when a row names that builtin.
Unnamed `notify` rows are all named `notify`, which is how a plain
`builtin = "notify"` row replaces its event's default.

Notification emission is the one fail-open config consumer: when configured
hook rows do not load, it logs and reports the error, then builds a registry
from the default layer only. Hook commands and other dispatchers still surface
the config error normally.

Override key is `(name, event)`. A higher-priority source row with the same key replaces the lower-priority row entirely. Use `meridian.local.toml` for machine-specific overrides without committing them.

## Event Classes and Failure Behavior

`EVENT_CLASS` maps each event to one of three classes:

| Class | Events | Default failure policy | Fail-open? |
|-------|--------|----------------------|-----------|
| `observe` | `spawn.created`, `spawn.running`, `spawn.start`, `work.start`, `work.started`, `session.responded`, `session.cache_expiring`, `notify.manual` | `warn` | Yes |
| `post` | `spawn.finalized`, `work.done` | `warn` | Yes |
| `gate` | (none currently registered) | `fail` | No |

`gate` class hooks stop the dispatch loop on failure. `observe` and `post` hooks are always fail-open: a hook failure logs a warning but does not stop subsequent hooks or the lifecycle event. Individual hooks can override with `failure_policy = "fail"`.

## Plugin API Boundary

**Built-in hooks take general capabilities from `meridian.plugin_api`**, the stable boundary external plugins also use; extend the API rather than reaching into `meridian.lib.*` for those. The exception is a tightly coupled companion a built-in wraps: `git-autosync`'s `autosync_store`, and `notify`'s `lib/notify` transports (imported lazily in `execute`). Those stay out of the plugin API because no external plugin should depend on them.

`HookContext` and `HookResult` are the plugin API's own classes, so built-ins receive the context the dispatcher built; `_to_plugin_hook()` in `dispatch.py` narrows the internal `Hook` row to the plugin `Hook`. Built-ins see only the plugin surface.

## Hook Execution Order

Within an event, hooks are sorted by:
1. Source rank (`default` → `user` → `project` → `local`, lower = earlier; `HOOK_SOURCE_PRECEDENCE` in `config.py`)
2. Negative priority (higher `priority` value = earlier execution)
3. Declaration order (stable sort)

## `HookWhen` Conditions

`when.status` filters by spawn terminal status (e.g., only fire on `success`). `when.agent` filters by agent profile name. Both are optional; if absent, the hook fires unconditionally for that event. Conditions are checked before interval throttling.

## Context Transport

`HookContext.to_env()` emits `MERIDIAN_HOOK_*`, `MERIDIAN_SPAWN_*`, `MERIDIAN_ACTIVE_WORK_*` env vars. `None` values are omitted (no empty string env vars). External hooks receive both env vars and JSON on stdin — the JSON schema includes a `schema_version` field (`HOOK_CONTEXT_SCHEMA_VERSION = 2`) for forward compatibility. Version 2 added the `notification` object (and `MERIDIAN_NOTIFICATION_TITLE/BODY/PRIORITY`) and made `runtime_root` nullable.

## `__init__.py` Lazy Loading

`lib/hooks/__init__.py` uses `__getattr__` with an `_EXPORTS` dispatch table to load submodules lazily. This avoids import-time circular dependencies. All types are available via `from meridian.lib.hooks import HookDispatcher` etc. — importers don't need to know the internal module structure.

## Related KB

→ KB: `$MERIDIAN_CONTEXT_KB_DIR/concepts/hooks-and-plugins.md` (see `meridian context kb`)
