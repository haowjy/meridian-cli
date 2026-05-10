# hooks/builtin/

In-process built-in hook implementations. These run inside the Meridian process
(no subprocess), but import only from `meridian.plugin_api` — not from
`meridian.lib.*`. This keeps them usable as a reference implementation for
external plugins.

## Files

- `base.py` — `BuiltinHook` protocol: `name`, `requirements`, `default_events`,
  `default_interval`, `check_requirements()`, `execute()`
- `git_autosync.py` — `GitAutosync` / `GIT_AUTOSYNC` singleton: commit-first sync
  workflow (add → commit → fetch → pull --rebase → push)

## How Built-ins Are Invoked

The dispatcher in `lib/hooks/dispatch.py` calls `_run_builtin(hook, context)` which
looks up the registered `BuiltinHook` by name and calls `execute(context, config)`.
Built-in hooks receive a `PluginHookContext` (plugin API surface), not the internal
`HookContext`.

## Adding a Built-in

1. Implement `BuiltinHook` protocol in a new module here
2. Import only from `meridian.plugin_api` (zero `meridian.lib.*` imports)
3. Register the instance in `lib/hooks/dispatch.py`'s built-in registry

## GitAutosync Behavior

- **commit-first**: local changes are committed before fetch/pull to protect them
  from `pull --rebase` conflicts
- **local-only mode**: when no `remote` is configured, stops after commit (no
  fetch/pull/push)
- **conflict policy**: `"leave"` (default) keeps the rebase in-place; `"abort"`
  runs `git rebase --abort` and skips
- **always skips, never errors**: hook failures are returned as `HookResult` with
  `outcome="skipped"`, not exceptions — the dispatcher is fail-open for `observe`
  and `post` event classes

## Related

- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — plugin API boundary rule,
  event classes, failure behavior
- `../../plugin_api/` — the stable surface built-ins must import from
