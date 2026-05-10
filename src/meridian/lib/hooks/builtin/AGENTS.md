# hooks/builtin/ — In-Process Built-In Hook Implementations

Built-ins run inside the Meridian process — no subprocess overhead — but they deliberately behave like external plugins: they import only from `meridian.plugin_api`, not from `meridian.lib.*`. This constraint makes them usable as reference implementations and keeps the plugin API surface honest.

## Mental Model

The dispatcher calls `_run_builtin(hook, context)` when it encounters a hook with a name matching a registered `BuiltinHook`. Built-ins receive a `PluginHookContext` (plugin API surface), not the internal `HookContext`. The bridge happens in `dispatch.py` via `_to_plugin_context()`.

## What Lives Here

- `base.py` — `BuiltinHook` protocol: `name`, `requirements`, `default_events`, `default_interval`, `check_requirements()`, `execute(hook, context, config)`
- `git_autosync.py` — `GIT_AUTOSYNC` singleton: commit-first sync (add → commit → fetch → pull --rebase → push)

## GitAutosync Behavior

`GIT_AUTOSYNC` is the canonical example of a built-in hook. Key behaviors:
- **Commit-first**: local changes are committed before fetch/pull, protecting them from `pull --rebase` conflicts
- **Local-only mode**: when no remote is configured, stops after commit (skips fetch/pull/push)
- **Conflict policy**: `"leave"` (default) leaves rebase in place; `"abort"` runs `git rebase --abort` and skips
- **Always returns `HookResult`**: never raises. Failures emit `outcome="skipped"`, not exceptions — the dispatcher is fail-open for `observe` and `post` events

## Adding a Built-In

1. Implement the `BuiltinHook` protocol in a new module here
2. **Import only from `meridian.plugin_api`** — zero `meridian.lib.*` imports
3. Register the instance in `lib/hooks/dispatch.py`'s built-in registry (`BUILTIN_HOOKS`)

If you need something from `meridian.lib.*`, that's a signal to extend `meridian.plugin_api` instead.

## Related

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — plugin API boundary rule, event classes, failure behavior, execution order
→ `../../plugin_api/` — the stable surface all built-ins must import from
